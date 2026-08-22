import contextlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from http.client import HTTPResponse
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote

MARK = "::pool::"
SERVER = os.getenv("GH_POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("GH_POOL_WORKER_TOKEN", "dev-worker")
TASK = os.getenv("GH_POOL_TASK")

_lock = threading.Lock()
_missing = object()


def emit(kind: str, value: Any = _missing, **fields: Any) -> dict[str, Any]:
    event = {"kind": kind, "at": round(time.time(), 3)}
    if value is not _missing:
        event["value"] = value
    event.update(fields)
    line = MARK + json.dumps(event, default=str, ensure_ascii=False) + "\n"
    with _lock:
        sys.stdout.write(line)
        sys.stdout.flush()
    return event


def _open(
    data: Path | str | bytes | bytearray | IO[bytes],
) -> tuple[IO[bytes] | bytes | bytearray, int]:
    if isinstance(data, Path):
        return data.open("rb"), data.stat().st_size
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, (bytes, bytearray)):
        data = data.read()
    return data, len(data)


def _call(
    key: str,
    method: str,
    data: IO[bytes] | bytes | bytearray | None = None,
    size: int | None = None,
    query: str = "",
) -> HTTPResponse:
    req = urllib.request.Request(  # noqa: S310
        f"{SERVER}/v1/artifacts/{quote(str(key), safe='/')}{query}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": str(size)}
        if data
        else {"Authorization": f"Bearer {TOKEN}"},
    )
    return urllib.request.urlopen(req, timeout=300)  # noqa: S310


def put(
    key: str,
    data: Path | str | bytes | bytearray | IO[bytes],
    task_id: str | None = None,
) -> dict[str, Any]:
    body, size = _open(data)
    tid = task_id or TASK
    try:
        with _call(key, "PUT", body, size, f"?task_id={tid}" if tid else "") as r:
            meta = json.loads(r.read())
    finally:
        if not isinstance(body, (bytes, bytearray)):
            body.close()
    emit("artifact", key=key, size=meta["size"], sha256=meta["sha256"])
    return meta


def get(key: str) -> bytes:
    with _call(key, "GET") as r:
        return r.read()


def download(key: str, path: str | Path) -> str | Path:
    with _call(key, "GET") as r, Path(path).open("wb") as f:
        shutil.copyfileobj(r, f)
    return path


def parse(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if line.startswith(MARK):
            with contextlib.suppress(ValueError):
                out.append(json.loads(line[len(MARK) :]))
    return out
