import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.chrome.e2e.site import Site
from tests.chrome.e2e.stack import (
    TOKEN,
    Cluster,
    Server,
    Stack,
    missing_desktop_tool,
    start_cluster,
)

from gh_pool.client import Session

NO_DATABASE = (
    "no postgres to test against: install one, or point "
    "GH_CHROME_TEST_DATABASE_URL at a cluster"
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "browser: drives a real Chrome on a real X display"
    )
    config.addinivalue_line("markers", "recording: needs ffmpeg to capture the screen")
    config.addinivalue_line(
        "markers", "patient_watchdog: wants a server that does not hurry a session"
    )


@pytest.fixture(scope="session")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    started = start_cluster(tmp_path_factory.mktemp("postgres"))
    if started is None:
        pytest.skip(NO_DATABASE)
    try:
        yield started
    finally:
        started.stop()


@pytest.fixture(scope="session")
def site() -> Iterator[Site]:
    running = Site()
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture(autouse=True)
def fresh_site(site: Site) -> Iterator[None]:
    site.reset()
    yield
    site.reset()


@pytest.fixture
def database(cluster: Cluster) -> Iterator[str]:
    name = f"gh_chrome_e2e_{uuid.uuid4().hex[:12]}"
    url = cluster.create(name)
    try:
        yield url
    finally:
        cluster.drop(name)


@pytest.fixture
def server_options() -> dict[str, Any]:
    return {}


@pytest.fixture
def server(
    database: str, tmp_path: Path, server_options: dict[str, Any]
) -> Iterator[Server]:
    running = Server(database, tmp_path / "storage")
    running.start(**server_options)
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
async def api(server: Server) -> AsyncIterator[httpx.AsyncClient]:

    async def as_the_runner(request: httpx.Request) -> None:
        parts = request.url.path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "runner":
            return
        try:
            session_id = uuid.UUID(parts[1])
        except ValueError:
            return
        token = server.runner_tokens.get(session_id)
        if token:
            request.headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(
        base_url=server.url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30.0,
        event_hooks={"request": [as_the_runner]},
    ) as client:
        yield client


@pytest.fixture
async def stack(server: Server, tmp_path: Path) -> AsyncIterator[Stack]:
    async with Stack(server, tmp_path / "runners") as running:
        yield running


@pytest.fixture
async def player(server: Server) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=server.url, auth=("admin", TOKEN), timeout=30.0
    ) as client:
        yield client


@pytest.fixture
def desktop() -> None:
    missing = missing_desktop_tool()
    if missing is not None:
        pytest.skip(f"{missing} is not installed")


@pytest.fixture
def ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")


@pytest.fixture
def zstd() -> None:
    if shutil.which("zstd") is None:
        pytest.skip("zstd is not installed")


@pytest.fixture
async def live(stack: Stack, desktop: None) -> Session:
    return await stack.live()
