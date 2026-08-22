from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from gh_pool.server import storage
from gh_pool.server.config import settings


async def _chunks(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


async def _failing(before: bytes) -> AsyncIterator[bytes]:
    yield before
    raise ConnectionResetError("the sender went away")


def _leftovers(directory: Path) -> list[str]:
    return [path.name for path in directory.iterdir()]


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "storage", tmp_path)
    storage.ensure_dirs()
    return tmp_path


def test_the_directories_a_server_needs_are_made_once(storage_root: Path):
    storage.ensure_dirs()

    assert (storage_root / "sessions").is_dir()
    assert (storage_root / "profiles").is_dir()
    assert (storage_root / "files").is_dir()


async def test_a_written_file_holds_every_chunk_in_order(tmp_path: Path):
    target = tmp_path / "deep" / "down" / "payload.bin"

    size = await storage.write_atomic(target, _chunks(b"first", b"second", b"third"))

    assert size == len(b"firstsecondthird")
    assert target.read_bytes() == b"firstsecondthird"


async def test_a_write_replaces_what_was_there_before(tmp_path: Path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"the old one")

    await storage.write_atomic(target, _chunks(b"the new one"))

    assert target.read_bytes() == b"the new one"
    assert _leftovers(tmp_path) == ["payload.bin"]


async def test_a_body_over_the_limit_is_refused_and_leaves_nothing_behind(
    tmp_path: Path,
):
    target = tmp_path / "payload.bin"

    with pytest.raises(storage.TooLarge):
        await storage.write_atomic(target, _chunks(b"x" * 10, b"y" * 10), limit=15)

    assert not target.exists()
    assert _leftovers(tmp_path) == []


async def test_a_body_that_stops_halfway_leaves_nothing_behind(tmp_path: Path):
    target = tmp_path / "payload.bin"

    with pytest.raises(ConnectionResetError):
        await storage.write_atomic(target, _failing(b"a start"))

    assert not target.exists()
    assert _leftovers(tmp_path) == []


async def test_a_failed_write_does_not_destroy_what_was_already_there(tmp_path: Path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"the one that matters")

    with pytest.raises(storage.TooLarge):
        await storage.write_atomic(target, _chunks(b"z" * 100), limit=10)

    assert target.read_bytes() == b"the one that matters"
    assert _leftovers(tmp_path) == ["payload.bin"]


async def test_a_write_that_cannot_be_put_in_place_leaves_no_temporary_file(
    tmp_path: Path,
):
    target = tmp_path / "payload.bin"
    target.mkdir()

    with pytest.raises(IsADirectoryError):
        await storage.write_atomic(target, _chunks(b"anything"))

    assert _leftovers(tmp_path) == ["payload.bin"]


async def test_a_body_exactly_on_the_limit_is_kept(tmp_path: Path):
    target = tmp_path / "payload.bin"

    size = await storage.write_atomic(target, _chunks(b"x" * 16), limit=16)

    assert size == 16


@pytest.mark.parametrize(
    "name", ["a-profile", "Profile.1", "a_b-c.d", "x", "9lives", "a" * 64]
)
def test_a_profile_name_that_is_a_filename_is_taken(storage_root: Path, name: str):
    assert storage.profile_path(name) == storage_root / "profiles" / f"{name}.tar.zst"


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "with space",
        ".hidden",
        "-leading-dash",
        "",
        "a" * 65,
        "sub/dir",
        "a\x00b",
    ],
)
def test_a_profile_name_that_is_not_one_never_becomes_a_path(
    storage_root: Path, name: str
):
    with pytest.raises(storage.BadName):
        storage.profile_path(name)


def test_everything_a_session_wrote_goes_when_the_session_does(storage_root: Path):
    session_id = uuid4()
    segments = storage.segments_dir(session_id)
    downloads = storage.downloads_dir(session_id)
    uploads = storage.files_dir(session_id)
    for directory in (segments, downloads, uploads):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "something").write_bytes(b"kept until now")

    storage.remove_session(session_id)

    assert not storage.session_dir(session_id).exists()
    assert not uploads.exists()


def test_forgetting_a_session_that_left_nothing_behind_is_not_an_error(
    storage_root: Path,
):
    storage.remove_session(uuid4())
