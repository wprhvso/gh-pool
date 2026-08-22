from pathlib import Path

import pytest

from gh_pool.client import EventType, RunnerError, Session, Topic
from gh_pool.protocol import Download
from tests.chrome.e2e.site import ASSET, Site
from tests.chrome.e2e.stack import Stack, Watch

pytestmark = pytest.mark.browser


async def test_a_file_from_disk_lands_in_the_file_input(
    live: Session, site: Site, tmp_path: Path
):
    source = tmp_path / "notes.txt"
    source.write_text("what the page will read")
    await live.goto(site.url("/upload"))

    await live.upload("#pick", path=source)

    await live.wait_for_function(
        "document.querySelector('#content').textContent.length > 0", timeout=30
    )
    assert await live.text("#content") == "notes.txt:what the page will read"


async def test_a_file_from_a_url_lands_in_the_file_input(live: Session, site: Site):
    await live.goto(site.url("/upload"))

    await live.upload("#pick", url=site.url("/asset/report.bin"))

    await live.wait_for_function(
        "document.querySelector('#content').textContent.length > 0", timeout=30
    )
    assert await live.text("#content") == f"report.bin:{ASSET.decode()}"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1:9222/json/version",
        "http://[::1]:9222/json/version",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_an_upload_url_the_runner_should_not_reach_is_refused(
    stack: Stack, site: Site, url: str, desktop: None
):
    session = await stack.live(runner_env={"GH_POOL_UPLOAD_ALLOW_PRIVATE": "0"})
    await session.goto(site.url("/upload"))

    with pytest.raises(RunnerError, match="upload will not fetch"):
        await session.upload("#pick", url=url)


async def test_upload_wants_exactly_one_of_a_path_and_a_url(live: Session):
    with pytest.raises(ValueError, match="exactly one"):
        await live.upload("#pick")


async def test_a_download_is_carried_back_to_the_server(
    stack: Stack, site: Site, tmp_path: Path, desktop: None
):
    session = await stack.live(subscribe=[Topic.DOWNLOADS])
    await session.goto(site.url("/download"))

    async with Watch(session) as watch:
        await session.click("#grab")
        event = await watch.wait_for(EventType.DOWNLOAD, timeout=60)

    assert isinstance(event, Download)
    assert event.name == "report.bin"
    assert event.size == len(ASSET)
    assert event.url == f"{stack.server.url}/sessions/{session.id}/downloads/report.bin"

    target = await session.download("report.bin", tmp_path / "back.bin")
    assert target.read_bytes() == ASSET


async def test_a_download_named_by_the_site_survives_the_trip_whole(
    stack: Stack, site: Site, tmp_path: Path, desktop: None
):
    session = await stack.live(subscribe=[Topic.DOWNLOADS])
    await session.goto(site.url("/download"))

    async with Watch(session) as watch:
        await session.click("#sneaky")
        event = await watch.wait_for(EventType.DOWNLOAD, timeout=60)

    assert isinstance(event, Download)
    assert event.name == "a report #1_v=2.bin"
    assert event.size == len(ASSET)

    target = await session.download(event.name, tmp_path / "back.bin")
    assert target.read_bytes() == ASSET
