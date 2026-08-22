import asyncio
import shutil
from pathlib import Path

import pytest

from gh_chrome_runner import profile
from gh_chrome_runner.config import settings


class FakeServer:
    def __init__(self, stored: Path) -> None:
        self.stored = stored
        self.put: list[str] = []
        self.has_archive = False

    async def put_file(self, path: str, source: Path) -> None:
        self.put.append(path)
        shutil.copyfile(source, self.stored)
        self.has_archive = True

    async def get_profile(self, target: Path) -> bool:
        if not self.has_archive:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.stored, target)
        return True


@pytest.fixture
def tools() -> None:
    for binary in ("tar", "zstd"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} is not installed")


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "workdir", tmp_path / "runner")
    settings.workdir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def server(tmp_path: Path) -> FakeServer:
    return FakeServer(tmp_path / "kept.tar.zst")


async def _listing(archive: Path) -> str:
    packed = await asyncio.create_subprocess_exec(
        "tar",
        "-I",
        "zstd -d",
        "-tf",
        str(archive),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await packed.communicate()
    return out.decode()


def _profile_with(files: dict[str, str]) -> None:
    for name, content in files.items():
        path = settings.profile_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


async def test_a_profile_is_stored_and_comes_back_as_it_was(
    tools: None, workdir: Path, server: FakeServer
):
    _profile_with({"Default/Cookies": "the cookie jar", "Local State": "{}"})

    await profile.store(server)  # pyright: ignore[reportArgumentType]
    shutil.rmtree(settings.profile_dir)
    restored = await profile.restore(server)  # pyright: ignore[reportArgumentType]

    assert server.put == ["profile"]
    assert restored is True
    assert (
        settings.profile_dir / "Default" / "Cookies"
    ).read_text() == "the cookie jar"
    assert (settings.profile_dir / "Local State").read_text() == "{}"


async def test_what_the_browser_can_rebuild_is_not_carried_between_sessions(
    tools: None, workdir: Path, server: FakeServer
):
    _profile_with(
        {
            "Default/Cookies": "worth keeping",
            "Default/Cache/big.bin": "not worth carrying",
            "ShaderCache/entry": "not worth carrying",
        }
    )

    await profile.store(server)  # pyright: ignore[reportArgumentType]

    listing = await _listing(server.stored)
    assert "Default/Cookies" in listing
    assert "Cache/big.bin" not in listing
    assert "ShaderCache" not in listing


async def test_a_session_with_no_archive_yet_starts_from_nothing(
    tools: None, workdir: Path, server: FakeServer
):
    assert await profile.restore(server) is False  # pyright: ignore[reportArgumentType]


async def test_restoring_replaces_whatever_the_runner_had(
    tools: None, workdir: Path, server: FakeServer
):
    _profile_with({"Default/Cookies": "the ones that came with the archive"})
    await profile.store(server)  # pyright: ignore[reportArgumentType]
    _profile_with({"Default/Cookies": "stale", "Default/Leftover": "from before"})

    await profile.restore(server)  # pyright: ignore[reportArgumentType]

    assert (
        settings.profile_dir / "Default" / "Cookies"
    ).read_text() == "the ones that came with the archive"
    assert not (settings.profile_dir / "Default" / "Leftover").exists()


async def test_the_archive_is_not_left_behind_on_the_runner(
    tools: None, workdir: Path, server: FakeServer
):
    _profile_with({"Local State": "{}"})

    await profile.store(server)  # pyright: ignore[reportArgumentType]
    await profile.restore(server)  # pyright: ignore[reportArgumentType]

    assert not (settings.workdir / profile.ARCHIVE).exists()


async def test_a_profile_that_will_not_pack_is_reported(
    tools: None, workdir: Path, server: FakeServer
):
    with pytest.raises(RuntimeError, match="tar"):
        await profile.store(server)  # pyright: ignore[reportArgumentType]
