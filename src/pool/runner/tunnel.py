import asyncio
import contextlib
import logging
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
import websockets
from pydantic import BaseModel
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin, Subprotocol

from pool.protocol import tunnel
from pool.runner.config import settings

log = logging.getLogger(__name__)

MIN_BACKOFF = 0.5
MAX_BACKOFF = 10.0
SETTLED = 30.0
CONNECT_TIMEOUT = 20.0
QUEUE_SIZE = 256
REQUEST_TIMEOUT = httpx.Timeout(30.0, read=300.0)


def socket_url(session_id: UUID) -> str:
    parts = urlsplit(settings.url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path}/runner/{session_id}/tunnel"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class TunnelOverflow(Exception):
    pass


class Channel:
    def __init__(self, link: "Link", stream_id: int) -> None:
        self.id = stream_id
        self._link = link
        self.overflowed = False
        self._queue: asyncio.Queue[tuple[tunnel.Op, bytes] | None] = asyncio.Queue(
            QUEUE_SIZE
        )

    async def read(self) -> tuple[tunnel.Op, bytes] | None:
        return await self._queue.get()

    async def send(self, op: tunnel.Op, payload: bytes | BaseModel = b"") -> None:
        await self._link.send(op, self.id, payload)

    def offer(self, op: tunnel.Op, payload: bytes) -> None:
        item = None if op in (tunnel.Op.EOF, tunnel.Op.CLOSE) else (op, payload)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True
            self.hang_up()

    def hang_up(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


class Link:
    def __init__(self, socket: ClientConnection, port: int) -> None:
        self._socket = socket
        self._port = port
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=REQUEST_TIMEOUT
        )
        self._channels: dict[int, Channel] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def pump(self) -> None:
        async for raw in self._socket:
            if isinstance(raw, str):
                continue
            op, stream_id, payload = tunnel.parse(raw)
            if op is None:
                continue
            if op is tunnel.Op.OPEN:
                self._accept(stream_id, tunnel.Open.model_validate_json(payload))
            elif (channel := self._channels.get(stream_id)) is not None:
                channel.offer(op, payload)

    async def shutdown(self) -> None:
        for channel in tuple(self._channels.values()):
            channel.hang_up()
        for task in tuple(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._client.aclose()

    async def send(
        self, op: tunnel.Op, stream_id: int, payload: bytes | BaseModel
    ) -> None:
        async with self._lock:
            await self._socket.send(tunnel.frame(op, stream_id, payload))

    def _accept(self, stream_id: int, message: tunnel.Open) -> None:
        channel = Channel(self, stream_id)
        self._channels[stream_id] = channel
        task = asyncio.create_task(self._serve(channel, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, channel: Channel, message: tunnel.Open) -> None:
        error: str | None = None
        try:
            if message.kind is tunnel.Kind.WS:
                await self._websocket(channel, message)
            else:
                await self._request(channel, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("desktop stream %d failed: %s", channel.id, error)
        finally:
            self._channels.pop(channel.id, None)
            with contextlib.suppress(Exception):
                await channel.send(tunnel.Op.CLOSE, tunnel.Close(error=error))

    async def _request(self, channel: Channel, message: tunnel.Open) -> None:
        body = bytearray()
        while (item := await channel.read()) is not None:
            body += item[1]
        if channel.overflowed:
            raise TunnelOverflow("the request body arrived faster than it was read")
        async with self._client.stream(
            message.method,
            message.target,
            headers=message.headers,
            content=bytes(body),
        ) as response:
            await channel.send(
                tunnel.Op.HEAD,
                tunnel.Head(
                    status=response.status_code,
                    headers=list(response.headers.multi_items()),
                ),
            )
            async for piece in response.aiter_raw():
                await channel.send(tunnel.Op.DATA, piece)

    async def _websocket(self, channel: Channel, message: tunnel.Open) -> None:
        url = f"ws://127.0.0.1:{self._port}{message.target}"
        offered = message.subprotocols or [tunnel.BINARY]
        try:
            upstream = await websockets.connect(
                url,
                origin=Origin(f"http://127.0.0.1:{self._port}"),
                subprotocols=[Subprotocol(name) for name in offered],
                max_size=tunnel.MAX_PAYLOAD,
                open_timeout=CONNECT_TIMEOUT,
                ping_interval=None,
            )
        except InvalidStatus as exc:
            await channel.send(
                tunnel.Op.HEAD, tunnel.Head(status=exc.response.status_code)
            )
            return
        await channel.send(
            tunnel.Op.HEAD, tunnel.Head(status=101, subprotocol=upstream.subprotocol)
        )
        down = asyncio.create_task(self._drain(channel, upstream))
        try:
            while (item := await channel.read()) is not None:
                op, payload = item
                await upstream.send(
                    payload.decode() if op is tunnel.Op.TEXT else payload
                )
        finally:
            down.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await down
            await upstream.close()

    async def _drain(self, channel: Channel, upstream: ClientConnection) -> None:
        try:
            async for raw in upstream:
                if isinstance(raw, str):
                    await channel.send(tunnel.Op.TEXT, raw.encode())
                else:
                    await channel.send(tunnel.Op.DATA, raw)
        finally:
            channel.hang_up()


class Tunnel:
    def __init__(self, session_id: UUID, port: int) -> None:
        self._url = socket_url(session_id)
        self._port = port
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = MIN_BACKOFF
        while True:
            started = loop.time()
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("tunnel is down: %s", exc)
            lived = loop.time() - started
            backoff = MIN_BACKOFF if lived > SETTLED else min(backoff * 2, MAX_BACKOFF)
            await asyncio.sleep(backoff)

    async def _connect(self) -> None:
        async with websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {settings.token}"},
            max_size=tunnel.MAX_PAYLOAD,
            open_timeout=CONNECT_TIMEOUT,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            log.info("tunnel connected to %s", self._url)
            link = Link(socket, self._port)
            try:
                await link.pump()
            finally:
                await link.shutdown()
