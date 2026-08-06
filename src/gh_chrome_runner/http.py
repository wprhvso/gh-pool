from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from gh_chrome_protocol import CommandError, EventData, RunnerConfig

from gh_chrome_runner.config import settings

CHUNK = 1 << 20


class ServerClient:
    def __init__(self, session_id: UUID) -> None:
        self._id = session_id
        self._client = httpx.AsyncClient(
            base_url=settings.url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=httpx.Timeout(60.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def config(self) -> RunnerConfig:
        response = await self._client.get(f"/runner/{self._id}/config")
        response.raise_for_status()
        return RunnerConfig.model_validate(response.json())

    @asynccontextmanager
    async def stream(self) -> AsyncGenerator[AsyncIterator[bytes]]:
        async with self._client.stream(
            "GET",
            f"/runner/{self._id}/stream",
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(30.0, read=None),
        ) as response:
            response.raise_for_status()
            yield response.aiter_bytes()

    async def complete(
        self, command_id: UUID, result: Any = None, error: CommandError | None = None
    ) -> None:
        payload = {
            "command_id": str(command_id),
            "result": result,
            "error": error.model_dump(mode="json") if error is not None else None,
        }
        response = await self._client.post(
            f"/runner/{self._id}/commands/{command_id}", json=payload
        )
        response.raise_for_status()

    async def heartbeat(self) -> bool:
        response = await self._client.post(f"/runner/{self._id}/heartbeat")
        return response.status_code == int(httpx.codes.OK)

    async def event(self, data: EventData) -> None:
        response = await self._client.post(
            f"/runner/{self._id}/events", json={"data": data.model_dump(mode="json")}
        )
        response.raise_for_status()

    async def confirm_close(self) -> None:
        response = await self._client.post(f"/runner/{self._id}/closed")
        response.raise_for_status()

    async def put_file(self, path: str, source: Path) -> None:
        with source.open("rb") as handle:
            response = await self._client.put(
                f"/runner/{self._id}/{path}",
                content=_iter_file(handle),
                headers={"Content-Type": "application/octet-stream"},
                timeout=httpx.Timeout(600.0),
            )
        response.raise_for_status()

    async def get_profile(self, target: Path) -> bool:
        async with self._client.stream(
            "GET", f"/runner/{self._id}/profile", timeout=httpx.Timeout(600.0)
        ) as response:
            if response.status_code == int(httpx.codes.NOT_FOUND):
                return False
            response.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
        return True

    async def get_upload(self, file_id: str, target: Path) -> Path:
        async with self._client.stream(
            "GET", f"/runner/{self._id}/files/{file_id}", timeout=httpx.Timeout(600.0)
        ) as response:
            response.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
        return target


def _iter_file(handle: Any) -> Any:
    while chunk := handle.read(CHUNK):
        yield chunk
