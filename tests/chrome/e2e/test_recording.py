from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest

from gh_pool.client import Session
from tests.chrome.e2e.site import Site
from tests.chrome.e2e.stack import Server, Stack, until

pytestmark = [pytest.mark.browser, pytest.mark.recording]

DASH = "{urn:mpeg:dash:schema:mpd:2011}"


def _segments(server: Server, session: Session) -> Path:
    return server.storage / "sessions" / str(session.id) / "seg"


async def _wait_for_segments(stack: Stack, session: Session, count: int) -> None:
    directory = _segments(stack.server, session)

    def arrived() -> bool:
        return (directory / "init.m4s").exists() and len(
            list(directory.glob("[0-9]*.m4s"))
        ) >= count

    try:
        await until(arrived, 120.0, f"{count} recorded segments")
    except TimeoutError:
        pytest.fail(f"no recording after 120s\n{stack.runners[-1].tail()}")


def _representation(manifest: str) -> ET.Element:
    root = ET.fromstring(manifest)  # noqa: S314
    found = root.find(f".//{DASH}Representation")
    assert found is not None
    return found


async def test_the_screen_is_recorded_and_offered_as_a_dash_stream(
    stack: Stack, site: Site, player: httpx.AsyncClient, desktop: None, ffmpeg: None
):
    session = await stack.live(width=800, height=600, fps=5)
    await session.goto(site.url("/tall"))
    await session.scroll_by(400)

    await _wait_for_segments(stack, session, count=2)

    manifest = await player.get(f"/s/{session.id}/manifest.mpd")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/dash+xml")
    representation = _representation(manifest.text)
    assert representation.get("width") == "800"
    assert representation.get("height") == "600"
    assert representation.get("frameRate") == "5"
    assert 'type="dynamic"' in manifest.text

    init = await player.get(f"/s/{session.id}/init.m4s")
    assert init.status_code == 200
    assert init.content[4:8] == b"ftyp"

    first = await player.get(f"/s/{session.id}/1.m4s")
    assert first.status_code == 200
    assert len(first.content) > 0


async def test_the_recording_is_sealed_when_the_session_ends(
    stack: Stack, site: Site, player: httpx.AsyncClient, desktop: None, ffmpeg: None
):
    session = await stack.live(width=640, height=480, fps=5)
    await session.goto(site.url("/form"))
    await _wait_for_segments(stack, session, count=1)

    await session.close()

    manifest = await player.get(f"/s/{session.id}/manifest.mpd")
    assert 'type="static"' in manifest.text
    assert "mediaPresentationDuration" in manifest.text


async def test_the_player_page_belongs_to_whoever_has_the_credentials(
    stack: Stack, player: httpx.AsyncClient, desktop: None, ffmpeg: None
):
    session = await stack.live(width=640, height=480, fps=5)

    async with httpx.AsyncClient(base_url=stack.server.url, timeout=30.0) as stranger:
        refused = await stranger.get(f"/s/{session.id}")
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"].startswith("Basic")

    page = await player.get(f"/s/{session.id}")
    assert page.status_code == 200
    assert str(session.id) in page.text
    assert page.cookies["gh_chrome_ticket"]
    assert session.player_url == f"{stack.server.url}/s/{session.id}"


async def test_a_session_with_nothing_recorded_yet_has_no_manifest(
    stack: Stack, player: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    missing = await player.get(f"/s/{session.id}/manifest.mpd")

    assert missing.status_code == 404
    assert (await player.get(f"/s/{session.id}/init.m4s")).status_code == 404
