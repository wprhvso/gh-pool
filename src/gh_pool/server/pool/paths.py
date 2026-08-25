import hashlib
from pathlib import Path

from gh_pool.core.config import settings


def task_dir(tid: str) -> Path:
    return settings.data_dir / tid


def events_path(tid: str) -> Path:
    return task_dir(tid) / "events.txt"


def events_size(tid: str) -> int:
    p = events_path(tid)
    return p.stat().st_size if p.exists() else 0


def blob_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()
    return settings.blobs_dir / h[:2] / h
