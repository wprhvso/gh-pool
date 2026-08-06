from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from gh_chrome_protocol import (
    CommandAccepted,
    CommandArgs,
    CommandRequest,
    ProfileInfo,
    SessionCreate,
    SessionState,
)

from gh_chrome.errors import (
    GhChromeError,
    SessionUnavailable,
    TooManySessions,
)

DEFAULT_URL = "http://127.0.0.1:8000"


def _raise(response: httpx.Response) -> None:
    if response.status_code == int(httpx.codes.TOO_MANY_REQUESTS):
        raise TooManySessions(response.text)
    if response.status_code == int(httpx.codes.CONFLICT):
        raise SessionUnavailable(response.text)
    if response.is_error:
        raise GhChromeError(f"{response.status_code}: {response.text[:300]}")


class Http:
    def __init__(self, server: str | None = None, token: str | None = None) -> None:
        base = server or os.environ.get("GH_CHROME_URL", DEFAULT_URL)
        secret = token if token is not None else os.environ.get("GH_CHROME_TOKEN", "")
        if not secret:
            raise GhChromeError("GH_CHROME_TOKEN is not set")
        self._client = httpx.AsyncClient(
            base_url=base.rstrip("/"),
            headers={"Authorization": f"Bearer {secret}"},
            timeout=httpx.Timeout(30.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_session(self, request: SessionCreate) -> SessionState:
        response = await self._client.post("/sessions", json=request.model_dump(mode="json"))
        _raise(response)
        return SessionState.model_validate(response.json())

    async def get_session(self, session_id: UUID) -> SessionState:
        response = await self._client.get(f"/sessions/{session_id}")
        _raise(response)
        return SessionState.model_validate(response.json())

    async def enqueue(
        self, session_id: UUID, args: CommandArgs, timeout: float | None
    ) -> CommandAccepted:
        request = CommandRequest(args=args, timeout=timeout)
        response = await self._client.post(
            f"/sessions/{session_id}/commands", json=request.model_dump(mode="json")
        )
        _raise(response)
        return CommandAccepted.model_validate(response.json())

    async def close_session(self, session_id: UUID) -> None:
        response = await self._client.post(f"/sessions/{session_id}/close")
        if response.status_code in {int(httpx.codes.NOT_FOUND), int(httpx.codes.CONFLICT)}:
            return
        _raise(response)

    async def delete_session(self, session_id: UUID) -> None:
        response = await self._client.delete(f"/sessions/{session_id}")
        _raise(response)

    async def upload_file(self, session_id: UUID, path: Path) -> UUID:
        with path.open("rb") as handle:
            response = await self._client.post(
                f"/sessions/{session_id}/files", files={"file": (path.name, handle)}
            )
        _raise(response)
        return UUID(response.json()["file_id"])

    async def download(self, session_id: UUID, name: str, target: Path) -> Path:
        async with self._client.stream(
            "GET", f"/sessions/{session_id}/downloads/{name}"
        ) as response:
            _raise(response)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
        return target

    async def profiles(self) -> list[ProfileInfo]:
        response = await self._client.get("/profiles")
        _raise(response)
        return [ProfileInfo.model_validate(item) for item in response.json()]

    async def delete_profile(self, name: str) -> None:
        response = await self._client.delete(f"/profiles/{name}")
        _raise(response)

    @asynccontextmanager
    async def events(self, session_id: UUID, last_seq: int) -> AsyncGenerator[AsyncIterator[bytes]]:
        async with self._client.stream(
            "GET",
            f"/sessions/{session_id}/events",
            params={"last_seq": last_seq},
            headers={"Last-Event-ID": str(last_seq), "Accept": "text/event-stream"},
            timeout=httpx.Timeout(30.0, read=None),
        ) as response:
            _raise(response)
            yield response.aiter_bytes()
