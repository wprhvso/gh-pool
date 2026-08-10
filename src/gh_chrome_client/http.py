import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from uuid import UUID

import httpx

from gh_chrome_client.errors import GhChromeError, SessionUnavailable, TooManySessions
from gh_chrome_protocol import (
    CommandAccepted,
    CommandArgs,
    CommandRequest,
    ProfileInfo,
    SessionCreate,
    SessionState,
)

DEFAULT_URL = "http://127.0.0.1:8000"


def _check(response: httpx.Response) -> httpx.Response:
    if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        raise TooManySessions(response.text)
    if response.status_code == HTTPStatus.CONFLICT:
        raise SessionUnavailable(response.text)
    if response.is_error:
        raise GhChromeError(f"{response.status_code}: {response.text[:300]}")
    return response


async def _check_stream(response: httpx.Response) -> None:
    """Same, for a streaming response whose body has not been read yet."""
    if response.is_error:
        await response.aread()
        _check(response)


class Http:
    """The HTTPS side of the client: POST upstream, server-sent events downstream."""

    def __init__(self, server: str | None = None, token: str | None = None) -> None:
        self.base_url = (server or os.environ.get("GH_CHROME_URL", DEFAULT_URL)).rstrip(
            "/"
        )
        secret = token if token is not None else os.environ.get("GH_CHROME_TOKEN", "")
        if not secret:
            raise GhChromeError("GH_CHROME_TOKEN is not set")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=httpx.Timeout(30.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_session(self, request: SessionCreate) -> SessionState:
        response = await self._client.post(
            "/sessions", json=request.model_dump(mode="json")
        )
        return SessionState.model_validate(_check(response).json())

    async def enqueue(
        self, session_id: UUID, args: CommandArgs, timeout: float | None
    ) -> CommandAccepted:
        request = CommandRequest(args=args, timeout=timeout)
        response = await self._client.post(
            f"/sessions/{session_id}/commands", json=request.model_dump(mode="json")
        )
        return CommandAccepted.model_validate(_check(response).json())

    async def close_session(self, session_id: UUID) -> None:
        response = await self._client.post(f"/sessions/{session_id}/close")
        if response.status_code not in {httpx.codes.NOT_FOUND, httpx.codes.CONFLICT}:
            _check(response)

    async def upload_file(self, session_id: UUID, path: Path) -> UUID:
        with path.open("rb") as handle:
            response = await self._client.post(
                f"/sessions/{session_id}/files", files={"file": (path.name, handle)}
            )
        return UUID(_check(response).json()["file_id"])

    async def download(self, session_id: UUID, name: str, target: Path) -> Path:
        url = f"/sessions/{session_id}/downloads/{name}"
        async with self._client.stream("GET", url) as response:
            await _check_stream(response)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
        return target

    async def profiles(self) -> list[ProfileInfo]:
        response = await self._client.get("/profiles")
        return [ProfileInfo.model_validate(item) for item in _check(response).json()]

    @asynccontextmanager
    async def events(
        self, session_id: UUID, last_seq: int
    ) -> AsyncGenerator[AsyncIterator[bytes]]:
        async with self._client.stream(
            "GET",
            f"/sessions/{session_id}/events",
            params={"last_seq": last_seq},
            headers={"Last-Event-ID": str(last_seq), "Accept": "text/event-stream"},
            timeout=httpx.Timeout(30.0, read=None),
        ) as response:
            await _check_stream(response)
            yield response.aiter_bytes()
