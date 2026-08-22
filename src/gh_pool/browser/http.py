from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx

from gh_pool.protocol import CommandError, EventData, RunnerConfig
from gh_pool.browser.config import settings

CHUNK = 1 << 20
TRANSFER_TIMEOUT = httpx.Timeout(600.0)


class ServerClient:
    def __init__(self, session_id: UUID) -> None:
        self._id = session_id
        self._client = httpx.AsyncClient(
            base_url=f"{settings.url.rstrip('/')}/runner/{session_id}",
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=httpx.Timeout(60.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def config(self) -> RunnerConfig:
        response = await self._client.get("/config")
        response.raise_for_status()
        return RunnerConfig.model_validate(response.json())

    @asynccontextmanager
    async def stream(self) -> AsyncGenerator[AsyncIterator[bytes]]:
        async with self._client.stream(
            "GET",
            "/stream",
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
        response = await self._client.post(f"/commands/{command_id}", json=payload)
        response.raise_for_status()

    async def heartbeat(self) -> bool:
        response = await self._client.post("/heartbeat")
        if response.status_code == HTTPStatus.CONFLICT:
            return False
        response.raise_for_status()
        return True

    async def event(self, data: EventData) -> None:
        response = await self._client.post(
            "/events", json={"data": data.model_dump(mode="json")}
        )
        response.raise_for_status()

    async def confirm_close(self) -> None:
        response = await self._client.post("/closed")
        response.raise_for_status()

    async def put_file(self, path: str, source: Path) -> None:
        response = await self._client.put(
            f"/{path}",
            content=_read_file(source),
            headers={"Content-Type": "application/octet-stream"},
            timeout=TRANSFER_TIMEOUT,
        )
        response.raise_for_status()

    async def get_profile(self, target: Path) -> bool:
        return await self._get_file("/profile", target) is not None

    async def get_upload(self, file_id: str, directory: Path) -> Path:
        async with self._client.stream(
            "GET", f"/files/{file_id}", timeout=TRANSFER_TIMEOUT
        ) as response:
            response.raise_for_status()
            return await _write(
                directory / file_id / _sent_name(response, file_id), response
            )

    async def _get_file(self, url: str, target: Path) -> Path | None:
        async with self._client.stream(
            "GET", url, timeout=TRANSFER_TIMEOUT
        ) as response:
            if response.status_code == HTTPStatus.NOT_FOUND:
                return None
            response.raise_for_status()
            return await _write(target, response)


async def _write(target: Path, response: httpx.Response) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        async for chunk in response.aiter_bytes():
            handle.write(chunk)
    return target


def _sent_name(response: httpx.Response, fallback: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    plain, _, extended = disposition.partition("filename*=")
    if extended:
        _, _, encoded = extended.split(";")[0].strip().rpartition("'")
        return Path(unquote(encoded)).name or fallback
    _, _, name = plain.partition("filename=")
    return Path(name.split(";")[0].strip(' "')).name or fallback


async def _read_file(source: Path) -> AsyncIterator[bytes]:
    with source.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk
