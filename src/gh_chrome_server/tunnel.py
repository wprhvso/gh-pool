import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import WebSocket
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from gh_chrome_protocol import tunnel

log = logging.getLogger(__name__)

QUEUE_SIZE = 256
HEAD_TIMEOUT = 30.0


class TunnelDown(Exception):
    pass


class Stream:
    def __init__(self, owner: "Tunnel", stream_id: int) -> None:
        self.id = stream_id
        self._owner = owner
        self._queue: asyncio.Queue[tuple[tunnel.Op, bytes]] = asyncio.Queue(QUEUE_SIZE)
        self._head: asyncio.Future[tunnel.Head] = (
            asyncio.get_running_loop().create_future()
        )
        self._gone = False

    async def head(self) -> tunnel.Head:
        async with asyncio.timeout(HEAD_TIMEOUT):
            return await self._head

    async def read(self) -> tuple[tunnel.Op, bytes] | None:
        op, payload = await self._queue.get()
        if op is tunnel.Op.CLOSE:
            error = tunnel.Close.model_validate_json(payload or b"{}").error
            if error is not None:
                raise TunnelDown(error)
            return None
        return op, payload

    async def body(self) -> AsyncIterator[bytes]:
        try:
            while (chunk := await self.read()) is not None:
                yield chunk[1]
        finally:
            await self.close()

    async def send(self, op: tunnel.Op, payload: bytes | BaseModel = b"") -> None:
        await self._owner.send(op, self.id, payload)

    async def close(self, error: str | None = None) -> None:
        if self._gone:
            return
        self._gone = True
        self._owner.forget(self.id)
        with contextlib.suppress(TunnelDown):
            await self._owner.send(tunnel.Op.CLOSE, self.id, tunnel.Close(error=error))

    async def offer(self, op: tunnel.Op, payload: bytes) -> None:
        if self._gone:
            return
        if op is tunnel.Op.HEAD:
            if not self._head.done():
                self._head.set_result(tunnel.Head.model_validate_json(payload))
            return
        try:
            self._queue.put_nowait((op, payload))
        except asyncio.QueueFull:
            reason = "the viewer fell too far behind"
            self.fail(reason)
            with contextlib.suppress(TunnelDown):
                await self._owner.send(
                    tunnel.Op.CLOSE, self.id, tunnel.Close(error=reason)
                )

    def fail(self, reason: str) -> None:
        self._gone = True
        self._owner.forget(self.id)
        if not self._head.done():
            self._head.set_exception(TunnelDown(reason))
            self._head.exception()  # a stream torn down before anyone waited on it
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(
            (tunnel.Op.CLOSE, tunnel.Close(error=reason).model_dump_json().encode())
        )


class Tunnel:
    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket
        self._streams: dict[int, Stream] = {}
        self._lock = asyncio.Lock()
        self._next = 0
        self.alive = True

    async def open(self, message: tunnel.Open) -> Stream:
        self._next += 1
        stream = Stream(self, self._next)
        self._streams[stream.id] = stream
        try:
            await self.send(tunnel.Op.OPEN, stream.id, message)
        except TunnelDown:
            self.forget(stream.id)
            raise
        return stream

    async def send(
        self, op: tunnel.Op, stream_id: int, payload: bytes | BaseModel = b""
    ) -> None:
        if not self.alive:
            raise TunnelDown("the runner is not connected")
        try:
            async with self._lock:
                await self._socket.send_bytes(tunnel.frame(op, stream_id, payload))
        except Exception as exc:
            self.alive = False
            raise TunnelDown("the tunnel dropped") from exc

    def forget(self, stream_id: int) -> None:
        self._streams.pop(stream_id, None)

    async def serve(self) -> None:
        try:
            while True:
                op, stream_id, payload = tunnel.parse(
                    await self._socket.receive_bytes()
                )
                stream = self._streams.get(stream_id)
                if stream is not None:
                    await stream.offer(op, payload)
        except WebSocketDisconnect:
            log.info("tunnel disconnected")
        except Exception:
            log.exception("tunnel failed")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.alive = False
        for stream in tuple(self._streams.values()):
            stream.fail("the runner went away")
        self._streams.clear()
        with contextlib.suppress(Exception):
            await self._socket.close()


class Tunnels:
    def __init__(self) -> None:
        self._live: dict[UUID, Tunnel] = {}

    def connected(self, session_id: UUID) -> bool:
        found = self._live.get(session_id)
        return found is not None and found.alive

    def get(self, session_id: UUID) -> Tunnel:
        found = self._live.get(session_id)
        if found is None or not found.alive:
            raise TunnelDown("the live desktop is not connected")
        return found

    async def serve(self, session_id: UUID, socket: WebSocket) -> None:
        previous = self._live.get(session_id)
        if previous is not None:
            await previous.shutdown()
        current = Tunnel(socket)
        self._live[session_id] = current
        try:
            await current.serve()
        finally:
            if self._live.get(session_id) is current:
                del self._live[session_id]
