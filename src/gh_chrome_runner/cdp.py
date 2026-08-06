from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)


class CdpError(Exception):
    def __init__(self, method: str, message: str) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message


class Cdp:
    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._socket: ClientConnection | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0

    @staticmethod
    async def version(port: int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/json/version")
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return payload

    async def connect(self) -> None:
        self._socket = await websockets.connect(
            self._endpoint, max_size=256 * 1024 * 1024, ping_interval=20
        )
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._socket is not None:
            await self._socket.close()
            self._socket = None

    def on(self, event: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def off(self, event: str) -> None:
        self._listeners.pop(event, None)

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        if self._socket is None:
            raise CdpError(method, "not connected")
        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._socket.send(json.dumps(message))
        return await future

    async def _read(self) -> None:
        assert self._socket is not None
        async for raw in self._socket:
            message = json.loads(raw)
            message_id = message.get("id")
            if message_id is not None:
                future = self._pending.pop(message_id, None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(
                        CdpError(str(message.get("method", "?")), message["error"]["message"])
                    )
                else:
                    future.set_result(message.get("result", {}))
                continue
            method = message.get("method")
            if method is None:
                continue
            for handler in self._listeners.get(method, ()):
                try:
                    handler(message)
                except Exception:
                    log.exception("cdp listener for %s failed", method)
