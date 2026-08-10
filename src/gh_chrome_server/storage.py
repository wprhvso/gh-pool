import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from gh_chrome_server.config import settings

CHUNK = 1 << 20


class BadName(ValueError):
    pass


def ensure_dirs() -> None:
    for path in (settings.sessions_dir, settings.profiles_dir, settings.files_dir):
        path.mkdir(parents=True, exist_ok=True)


def session_dir(session_id: UUID) -> Path:
    return settings.sessions_dir / str(session_id)


def segments_dir(session_id: UUID) -> Path:
    return session_dir(session_id) / "seg"


def downloads_dir(session_id: UUID) -> Path:
    return session_dir(session_id) / "downloads"


def files_dir(session_id: UUID) -> Path:
    return settings.files_dir / str(session_id)


def profile_path(name: str) -> Path:
    return settings.profiles_dir / f"{name}.tar.zst"


def safe_name(name: str) -> str:
    cleaned = Path(name).name
    if not cleaned or cleaned in {".", ".."}:
        raise BadName(name)
    return cleaned


async def write_atomic(target: Path, chunks: AsyncIterator[bytes]) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            async for chunk in chunks:
                tmp.write(chunk)
                size += len(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    temp_path.replace(target)
    return size


def remove_session(session_id: UUID) -> None:
    shutil.rmtree(session_dir(session_id), ignore_errors=True)
    shutil.rmtree(files_dir(session_id), ignore_errors=True)
