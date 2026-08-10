from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from gh_chrome_protocol import CommandError, EventData, RunnerConfig
from gh_chrome_runner.config import settings

CHUNK = 1 << 20
TRANSFER_TIMEOUT = httpx.Timeout(600.0)


class ServerClient:
    """The runner's side of the wire: one session, one server."""

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
        """The command stream; it stays open for the life of the session."""
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
        """False once the server has given up on this session."""
        return (await self._client.post("/heartbeat")).is_success

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
        """False when the server has no archive for this profile yet."""
        return await self._get_file("/profile", target) is not None

    async def get_upload(self, file_id: str, directory: Path) -> Path:
        """Fetch a file the client uploaded, keeping the name the client gave it."""
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
    """The file name out of Content-Disposition, which is what the page will see."""
    _, _, name = response.headers.get("content-disposition", "").partition("filename=")
    return Path(name.strip(' ";')).name or fallback


async def _read_file(source: Path) -> AsyncIterator[bytes]:
    with source.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk
