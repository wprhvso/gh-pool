from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import uvicorn
from gh_chrome_server import github
from gh_chrome_server.app import create_app
from gh_chrome_server.config import settings

TOKEN = "test-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session", autouse=True)
def configure(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    storage: Path = tmp_path_factory.mktemp("storage")
    settings.token = TOKEN
    settings.database_url = url
    settings.storage = storage
    settings.heartbeat_timeout = 3.0
    settings.ready_timeout = 30.0
    settings.watchdog_interval = 0.2
    yield


@pytest.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    async def no_dispatch(session_id: object) -> None:
        return None

    monkeypatch.setattr(github, "dispatch", no_dispatch)
    port = free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    while not instance.started:
        await asyncio.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("GH_CHROME_URL", base)
    monkeypatch.setenv("GH_CHROME_TOKEN", TOKEN)
    yield base
    instance.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        await task
