import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO
from uuid import UUID

from gh_pool.core.config import settings
from gh_pool.protocol import PROFILE_NAME

CHUNK = 1 << 20


class BadName(ValueError):
    pass


class TooLarge(ValueError):
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
    if re.fullmatch(PROFILE_NAME, name) is None:
        raise BadName(name)
    return settings.profiles_dir / f"{name}.tar.zst"


def safe_name(name: str) -> str:
    cleaned = Path(name).name
    if not cleaned or cleaned in {".", ".."}:
        raise BadName(name)
    return cleaned


async def write_atomic(
    target: Path, chunks: AsyncIterator[bytes], limit: int | None = None
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            async for chunk in chunks:
                await asyncio.to_thread(tmp.write, chunk)
                size += len(chunk)
                if limit is not None and size > limit:
                    raise TooLarge(f"more than {limit} bytes")
            await asyncio.to_thread(_persist, tmp)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    try:
        temp_path.replace(target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return size


def _persist(tmp: IO[bytes]) -> None:
    tmp.flush()
    os.fsync(tmp.fileno())


def remove_session(session_id: UUID) -> None:
    shutil.rmtree(session_dir(session_id), ignore_errors=True)
    shutil.rmtree(files_dir(session_id), ignore_errors=True)


def _tree_size(root: Path) -> int:
    total = 0
    for item in root.rglob("*"):
        with contextlib.suppress(OSError):
            if item.is_file():
                total += item.stat().st_size
    return total


def session_size(session_id: UUID) -> int:
    return _tree_size(session_dir(session_id)) + _tree_size(files_dir(session_id))


def sessions_size() -> int:
    return _tree_size(settings.sessions_dir) + _tree_size(settings.files_dir)
