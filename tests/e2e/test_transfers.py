import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from tests.e2e.stack import Stack, Watch, file_id_of

from gh_chrome_client import EventType, GhChromeError
from gh_chrome_protocol import CommandEnvelope, Download, Method
from gh_chrome_server import storage
from gh_chrome_server.storage import BadName

PAYLOAD = os.urandom(1 << 18)
MAX_UPLOAD = 1 << 20


@pytest.fixture
def server_options() -> dict[str, Any]:
    return {"max_upload": MAX_UPLOAD}


def _file(path: Path, name: str, content: bytes = PAYLOAD) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_bytes(content)
    return target


async def test_a_file_travels_from_the_client_to_the_runner(
    stack: Stack, tmp_path: Path
):
    session, runner = await stack.scripted()
    source = _file(tmp_path / "client", "payload.bin")
    fetched: list[Path] = []

    async def handler(envelope: CommandEnvelope) -> None:
        fetched.append(
            await runner.client.get_upload(file_id_of(envelope), tmp_path / "runner")
        )

    runner.on(Method.UPLOAD, handler)

    await session.upload("#pick", path=source)

    assert fetched[0].name == "payload.bin"
    assert fetched[0].read_bytes() == PAYLOAD


async def test_a_download_from_the_runner_reaches_the_client(
    stack: Stack, tmp_path: Path
):
    session, runner = await stack.scripted()
    source = _file(tmp_path / "runner", "report.bin")

    async with Watch(session) as watch:
        await runner.client.put_file("downloads/report.bin", source)
        announced = await watch.wait_for(EventType.DOWNLOAD)

    assert isinstance(announced, Download)
    assert announced.name == "report.bin"
    assert announced.size == len(PAYLOAD)
    assert (
        announced.url
        == f"{stack.server.url}/sessions/{session.id}/downloads/report.bin"
    )

    target = await session.download("report.bin", tmp_path / "back" / "report.bin")
    assert target.read_bytes() == PAYLOAD


async def test_a_download_that_was_never_made_is_a_404(stack: Stack, tmp_path: Path):
    session, _ = await stack.scripted()

    with pytest.raises(GhChromeError, match="404"):
        await session.download("nothing.bin", tmp_path / "nothing.bin")


async def test_a_body_bigger_than_the_server_will_keep_is_turned_away(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()
    too_much = b"x" * (stack.server.max_upload + 1)

    posted = await api.post(
        f"/sessions/{session.id}/files",
        files={"file": ("big.bin", too_much)},
    )
    put = await api.put(f"/runner/{session.id}/downloads/big.bin", content=too_much)

    assert posted.status_code == 413
    assert put.status_code == 413


async def test_a_download_named_to_climb_out_stays_where_it_belongs(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    climbing = await api.put(f"/runner/{session.id}/downloads/%2E%2E", content=b"nope")
    here = await api.put(f"/runner/{session.id}/downloads/%2E", content=b"nope")
    plain = await api.put(f"/runner/{session.id}/downloads/report.bin", content=b"fine")

    assert climbing.status_code == 400
    assert here.status_code == 400
    assert plain.status_code == 204
    downloads = stack.server.storage / "sessions" / str(session.id) / "downloads"
    assert [path.name for path in downloads.iterdir()] == ["report.bin"]


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("dir/report.bin", "report.bin"),
        ("report.bin", "report.bin"),
    ],
)
def test_a_name_with_directories_in_it_is_reduced_to_the_last_part(
    given: str, kept: str
):
    assert storage.safe_name(given) == kept


@pytest.mark.parametrize("given", ["..", ".", "", "/", "//"])
def test_a_name_that_is_no_name_at_all_is_refused(given: str):
    with pytest.raises(storage.BadName):
        storage.safe_name(given)


async def test_the_recording_is_stored_segment_by_segment(
    stack: Stack, api: httpx.AsyncClient, player: httpx.AsyncClient, tmp_path: Path
):
    session, runner = await stack.scripted()
    await runner.client.put_file("init", _file(tmp_path, "init.m4s", b"\0\0\0\x18ftyp"))
    for number in (1, 2, 3):
        await runner.client.put_file(
            f"segments/{number}", _file(tmp_path, f"{number}.m4s", b"segment")
        )

    manifest = await player.get(f"/s/{session.id}/manifest.mpd")

    assert manifest.status_code == 200
    assert manifest.text.count("SegmentTemplate") == 1
    assert 'startNumber="1"' in manifest.text
    assert (await player.get(f"/s/{session.id}/init.m4s")).content == b"\0\0\0\x18ftyp"
    assert (await player.get(f"/s/{session.id}/3.m4s")).content == b"segment"
    assert (await player.get(f"/s/{session.id}/9.m4s")).status_code == 404
    assert (
        await api.put(f"/runner/{session.id}/segments/0", content=b"x")
    ).status_code == 400


async def test_the_recording_is_not_public(stack: Stack):
    session, _ = await stack.scripted()

    async with httpx.AsyncClient(base_url=stack.server.url, timeout=30.0) as stranger:
        assert (await stranger.get(f"/s/{session.id}/manifest.mpd")).status_code == 401
        assert (await stranger.get(f"/s/{session.id}/init.m4s")).status_code == 401


async def test_a_profile_archive_survives_for_the_next_session(
    stack: Stack, api: httpx.AsyncClient, tmp_path: Path
):
    first, runner = await stack.scripted(profile="carried-over")
    assert runner.config is not None
    assert not runner.config.has_profile_archive

    await runner.client.put_file("profile", _file(tmp_path, "profile.tar.zst"))
    await first.close()

    _, later = await stack.scripted(profile="carried-over")
    assert later.config is not None
    assert later.config.has_profile_archive
    restored = tmp_path / "restored.tar.zst"
    assert await later.client.get_profile(restored)
    assert restored.read_bytes() == PAYLOAD

    listed = (await api.get("/profiles")).json()
    assert listed[0]["name"] == "carried-over"
    assert listed[0]["size"] == len(PAYLOAD)
    assert listed[0]["stale"] is False


async def test_a_profile_name_that_climbs_out_never_gets_a_session(
    stack: Stack, api: httpx.AsyncClient
):
    refused = await api.post("/sessions", json={"profile": "../../../../tmp/pwn"})

    assert refused.status_code == 422
    assert stack.server.dispatched == []
    with pytest.raises(BadName):
        storage.profile_path("../../../../tmp/pwn")


async def test_a_session_that_keeps_nothing_may_not_store_a_profile(
    stack: Stack, api: httpx.AsyncClient, tmp_path: Path
):
    session, runner = await stack.scripted(profile="read-only", persist=False)

    with pytest.raises(httpx.HTTPStatusError, match="409"):
        await runner.client.put_file("profile", _file(tmp_path, "profile.tar.zst"))

    assert (await api.get(f"/runner/{session.id}/profile")).status_code == 404


async def test_a_profile_can_be_forgotten(
    stack: Stack, api: httpx.AsyncClient, tmp_path: Path
):
    session, runner = await stack.scripted(profile="not-for-long")
    await runner.client.put_file("profile", _file(tmp_path, "profile.tar.zst"))
    await session.close()

    assert (await api.delete("/profiles/not-for-long")).status_code == 204

    assert (await api.get("/profiles")).json() == []
    assert not (stack.server.storage / "profiles" / "not-for-long.tar.zst").exists()


async def test_a_file_the_runner_asks_for_by_the_wrong_name_is_a_404(stack: Stack):
    _, runner = await stack.scripted()

    with pytest.raises(httpx.HTTPStatusError, match="404"):
        await runner.client.get_upload(str(uuid4()), Path("/tmp/nowhere"))
