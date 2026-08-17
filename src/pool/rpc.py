import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

MARK = "::pool::"
SERVER = os.getenv("POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("POOL_TOKEN", "dev-worker")
TASK = os.getenv("POOL_TASK")

_lock = threading.Lock()
_missing = object()


def emit(kind, value=_missing, **fields):
    event = {"kind": kind, "at": round(time.time(), 3)}
    if value is not _missing:
        event["value"] = value
    event.update(fields)
    line = MARK + json.dumps(event, default=str, ensure_ascii=False) + "\n"
    with _lock:
        sys.stdout.write(line)
        sys.stdout.flush()
    return event


def _open(data):
    if isinstance(data, Path):
        return data.open("rb"), data.stat().st_size
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, (bytes, bytearray)):
        data = data.read()
    return data, len(data)


def _call(key, method, data=None, size=None, query=""):
    req = urllib.request.Request(
        f"{SERVER}/v1/artifacts/{quote(str(key), safe='/')}{query}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": str(size)} if data else {"Authorization": f"Bearer {TOKEN}"},
    )
    return urllib.request.urlopen(req, timeout=300)


def put(key, data, task_id=None):
    body, size = _open(data)
    tid = task_id or TASK
    try:
        with _call(key, "PUT", body, size, f"?task_id={tid}" if tid else "") as r:
            meta = json.loads(r.read())
    finally:
        if hasattr(body, "close"):
            body.close()
    emit("artifact", key=key, size=meta["size"], sha256=meta["sha256"])
    return meta


def get(key):
    with _call(key, "GET") as r:
        return r.read()


def download(key, path):
    with _call(key, "GET") as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)
    return path


def parse(text):
    out = []
    for line in text.splitlines():
        if line.startswith(MARK):
            try:
                out.append(json.loads(line[len(MARK) :]))
            except ValueError:
                pass
    return out
