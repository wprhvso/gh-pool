from __future__ import annotations

import asyncio
import contextlib
import json
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx
from gh_chrome_protocol import CommandEnvelope, CommandError, ErrorCode, RunnerConfig
from gh_chrome_protocol.sse import parse_sse


class Mode(StrEnum):
    ECHO = "echo"
    SILENT = "silent"


class FakeRunner:
    def __init__(
        self,
        base_url: str,
        token: str,
        session_id: UUID,
        mode: Mode = Mode.ECHO,
        delay: float = 0.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0),
        )
        self._id = session_id
        self._mode = mode
        self._delay = delay
        self._tasks: list[asyncio.Task[None]] = []
        self.config: RunnerConfig | None = None
        self.seen: list[CommandEnvelope] = []
        self.cancelled: list[UUID] = []
        self.closed = asyncio.Event()

    async def start(self) -> None:
        response = await self._client.get(f"/runner/{self._id}/config")
        response.raise_for_status()
        self.config = RunnerConfig.model_validate(response.json())
        self._tasks.append(asyncio.create_task(self._beat()))
        self._tasks.append(asyncio.create_task(self._consume()))

    async def stop(self, confirm_close: bool = False) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if confirm_close:
            with contextlib.suppress(httpx.HTTPError):
                await self._client.post(f"/runner/{self._id}/closed")
        await self._client.aclose()

    async def _beat(self) -> None:
        while True:
            with contextlib.suppress(httpx.HTTPError):
                await self._client.post(f"/runner/{self._id}/heartbeat")
            await asyncio.sleep(1.0)

    async def _consume(self) -> None:
        async with self._client.stream(
            "GET",
            f"/runner/{self._id}/stream",
            timeout=httpx.Timeout(10.0, read=None),
        ) as response:
            response.raise_for_status()
            async for message in parse_sse(response.aiter_bytes()):
                if message.event == "close":
                    self.closed.set()
                    return
                if message.event == "cancel":
                    self.cancelled.append(UUID(json.loads(message.data)["command_id"]))
                    continue
                if message.event != "command":
                    continue
                envelope = CommandEnvelope.model_validate_json(message.data)
                self.seen.append(envelope)
                self._tasks.append(asyncio.create_task(self._execute(envelope)))

    async def _execute(self, envelope: CommandEnvelope) -> None:
        if self._mode is Mode.SILENT:
            return
        await asyncio.sleep(self._delay)
        payload: dict[str, Any] = {
            "command_id": str(envelope.command_id),
            "result": {"method": str(envelope.args.method)},
            "error": None,
        }
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(
                f"/runner/{self._id}/commands/{envelope.command_id}", json=payload
            )

    async def fail_next(self, command_id: UUID, code: ErrorCode, message: str) -> None:
        error = CommandError(code=code, message=message)
        await self._client.post(
            f"/runner/{self._id}/commands/{command_id}",
            json={
                "command_id": str(command_id),
                "result": None,
                "error": error.model_dump(mode="json"),
            },
        )
