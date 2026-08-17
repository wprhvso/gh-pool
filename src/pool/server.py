import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = os.getenv("DB_PATH", "./pool.db")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "dev-worker")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN", "dev-client")
LOG_CAP = int(os.getenv("LOG_CAP", str(100 * 1024 * 1024)))
LOST_AFTER = float(os.getenv("LOST_AFTER", "300"))
LEASE_WAIT = float(os.getenv("LEASE_WAIT", "30"))
WORKER_STALE = float(os.getenv("WORKER_STALE", "120"))

TERMINAL = ("done", "failed", "cancelled")

DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("PRAGMA busy_timeout=5000")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        worker_id TEXT,
        lease_token TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        result_name TEXT,
        parent_id TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        heartbeat_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_pending ON tasks(status, created_at);
    CREATE TABLE IF NOT EXISTS workers (
        id TEXT PRIMARY KEY,
        seen_at REAL NOT NULL,
        task_id TEXT
    );
    """
)

new_task = asyncio.Event()
log_locks: dict[str, asyncio.Lock] = {}


def q(sql, args=()):
    with _lock:
        return db.execute(sql, args).fetchall()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    with _lock:
        db.execute(sql, args)


def task_dir(tid):
    d = DATA_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path(tid):
    return task_dir(tid) / "log.txt"


def log_size(tid):
    p = log_path(tid)
    return p.stat().st_size if p.exists() else 0


def auth_worker(h):
    if h != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(401, "bad worker token")


def auth_client(h):
    if h != f"Bearer {CLIENT_TOKEN}":
        raise HTTPException(401, "bad client token")


def get_task(tid):
    row = one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not row:
        raise HTTPException(404, "no such task")
    return row


def owned(tid, lease_token):
    row = get_task(tid)
    if not lease_token or row["lease_token"] != lease_token:
        raise HTTPException(409, "stale lease")
    return row


def as_dict(row):
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["cancel_requested"] = bool(d["cancel_requested"])
    d["log_size"] = log_size(d["id"])
    return d


def grab(worker_id):
    with _lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT id, type, payload FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            token = uuid.uuid4().hex
            now = time.time()
            db.execute(
                "UPDATE tasks SET status='running', worker_id=?, lease_token=?, started_at=?, heartbeat_at=? WHERE id=?",
                (worker_id, token, now, now, row["id"]),
            )
            db.execute(
                "INSERT INTO workers(id, seen_at, task_id) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at, task_id=excluded.task_id",
                (worker_id, now, row["id"]),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return {
        "task_id": row["id"],
        "type": row["type"],
        "payload": json.loads(row["payload"]),
        "lease_token": token,
        "log_offset": log_size(row["id"]),
    }


async def reaper():
    while True:
        try:
            now = time.time()
            with _lock:
                db.execute(
                    "UPDATE tasks SET status='lost', finished_at=?, error='worker gone', lease_token=NULL "
                    "WHERE status='running' AND heartbeat_at < ?",
                    (now, now - LOST_AFTER),
                )
                db.execute("DELETE FROM workers WHERE seen_at < ?", (now - WORKER_STALE,))
        except Exception as e:
            print("reaper:", e)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    t = asyncio.create_task(reaper())
    yield
    t.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/v1/lease")
async def lease(request: Request, authorization: str = Header(None)):
    auth_worker(authorization)
    body = await request.json()
    worker_id = body.get("worker_id")
    if not worker_id:
        raise HTTPException(400, "worker_id required")
    now = time.time()
    run(
        "INSERT INTO workers(id, seen_at, task_id) VALUES(?,?,NULL) "
        "ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at, task_id=NULL",
        (worker_id, now),
    )
    deadline = time.monotonic() + LEASE_WAIT
    while True:
        t = grab(worker_id)
        if t:
            return t
        left = deadline - time.monotonic()
        if left <= 0:
            return Response(status_code=204)
        try:
            await asyncio.wait_for(new_task.wait(), timeout=left)
            new_task.clear()
        except asyncio.TimeoutError:
            return Response(status_code=204)


@app.post("/v1/tasks/{tid}/heartbeat")
async def heartbeat(tid: str, x_lease_token: str = Header(None), authorization: str = Header(None)):
    auth_worker(authorization)
    row = owned(tid, x_lease_token)
    now = time.time()
    run("UPDATE tasks SET heartbeat_at=? WHERE id=?", (now, tid))
    if row["worker_id"]:
        run("UPDATE workers SET seen_at=? WHERE id=?", (now, row["worker_id"]))
    return {"cancel": bool(row["cancel_requested"]), "log_offset": log_size(tid)}


@app.post("/v1/tasks/{tid}/log")
async def append_log(
    tid: str,
    request: Request,
    offset: int = Query(...),
    x_lease_token: str = Header(None),
    authorization: str = Header(None),
):
    auth_worker(authorization)
    owned(tid, x_lease_token)
    lock = log_locks.setdefault(tid, asyncio.Lock())
    async with lock:
        size = log_size(tid)
        if size >= LOG_CAP:
            return {"offset": size, "accepting": False}
        if offset != size:
            return JSONResponse({"offset": size, "accepting": True}, status_code=409)
        data = await request.body()
        if data:
            p = log_path(tid)

            def w():
                with open(p, "ab") as f:
                    f.write(data)
                    return f.tell()

            size = await anyio.to_thread.run_sync(w)
        return {"offset": size, "accepting": size < LOG_CAP}


@app.post("/v1/tasks/{tid}/result")
async def upload_result(
    tid: str,
    request: Request,
    filename: str = Query("result.bin"),
    x_lease_token: str = Header(None),
    authorization: str = Header(None),
):
    auth_worker(authorization)
    owned(tid, x_lease_token)
    safe = os.path.basename(filename) or "result.bin"
    final = task_dir(tid) / safe
    part = final.with_suffix(final.suffix + ".part")
    f = await anyio.to_thread.run_sync(lambda: open(part, "wb"))
    try:
        async for chunk in request.stream():
            if chunk:
                await anyio.to_thread.run_sync(f.write, chunk)
    finally:
        await anyio.to_thread.run_sync(f.close)
    await anyio.to_thread.run_sync(os.replace, part, final)
    run("UPDATE tasks SET result_name=? WHERE id=?", (safe, tid))
    return {"ok": True, "size": final.stat().st_size, "name": safe}


@app.post("/v1/tasks/{tid}/complete")
async def complete(
    tid: str,
    request: Request,
    x_lease_token: str = Header(None),
    authorization: str = Header(None),
):
    auth_worker(authorization)
    row = owned(tid, x_lease_token)
    body = await request.json()
    status = body.get("status", "done")
    if status not in ("done", "failed", "cancelled"):
        raise HTTPException(400, "bad status")
    if row["status"] in TERMINAL:
        return {"ok": True, "status": row["status"], "note": "already terminal"}
    run(
        "UPDATE tasks SET status=?, error=?, finished_at=?, lease_token=NULL WHERE id=?",
        (status, body.get("error"), time.time(), tid),
    )
    if row["worker_id"]:
        run("UPDATE workers SET task_id=NULL, seen_at=? WHERE id=?", (time.time(), row["worker_id"]))
    log_locks.pop(tid, None)
    return {"ok": True, "status": status}


@app.post("/v1/tasks")
async def create_task(request: Request, authorization: str = Header(None)):
    auth_client(authorization)
    body = await request.json()
    ttype = body.get("type")
    if not ttype:
        raise HTTPException(400, "type required")
    tid = uuid.uuid4().hex
    run(
        "INSERT INTO tasks(id, type, payload, status, created_at) VALUES(?,?,?,'pending',?)",
        (tid, ttype, json.dumps(body.get("payload", {})), time.time()),
    )
    new_task.set()
    return {"task_id": tid}


@app.get("/v1/tasks/{tid}")
async def task_status(tid: str, authorization: str = Header(None)):
    auth_client(authorization)
    return as_dict(get_task(tid))


@app.get("/v1/tasks")
async def list_tasks(
    status: str = Query(None),
    limit: int = Query(100),
    authorization: str = Header(None),
):
    auth_client(authorization)
    if status:
        rows = q(
            "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        rows = q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
    return [as_dict(r) for r in rows]


@app.get("/v1/tasks/{tid}/log")
async def read_log(tid: str, offset: int = Query(0), authorization: str = Header(None)):
    auth_client(authorization)
    row = get_task(tid)
    p = log_path(tid)
    size = p.stat().st_size if p.exists() else 0
    data = b""
    if offset < size:

        def r():
            with open(p, "rb") as f:
                f.seek(offset)
                return f.read()

        data = await anyio.to_thread.run_sync(r)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Log-Offset": str(offset + len(data)),
            "X-Task-Status": row["status"],
            "X-Log-Size": str(size),
        },
    )


@app.get("/v1/tasks/{tid}/result")
async def download_result(tid: str, authorization: str = Header(None)):
    auth_client(authorization)
    row = get_task(tid)
    if not row["result_name"]:
        raise HTTPException(404, "no result")
    p = task_dir(tid) / row["result_name"]
    if not p.exists():
        raise HTTPException(404, "no result file")
    return FileResponse(p, filename=row["result_name"])


@app.post("/v1/tasks/{tid}/cancel")
async def cancel(tid: str, authorization: str = Header(None)):
    auth_client(authorization)
    row = get_task(tid)
    if row["status"] == "pending":
        run(
            "UPDATE tasks SET status='cancelled', finished_at=?, error='cancelled before start' WHERE id=?",
            (time.time(), tid),
        )
        return {"status": "cancelled"}
    if row["status"] == "running":
        run("UPDATE tasks SET cancel_requested=1 WHERE id=?", (tid,))
        return {"status": "running", "cancel_requested": True}
    return {"status": row["status"], "note": "already terminal"}


@app.post("/v1/tasks/{tid}/retry")
async def retry(tid: str, authorization: str = Header(None)):
    auth_client(authorization)
    row = get_task(tid)
    nid = uuid.uuid4().hex
    run(
        "INSERT INTO tasks(id, type, payload, status, parent_id, created_at) VALUES(?,?,?,'pending',?,?)",
        (nid, row["type"], row["payload"], tid, time.time()),
    )
    new_task.set()
    return {"task_id": nid, "parent_id": tid}


@app.get("/v1/workers")
async def workers(authorization: str = Header(None)):
    auth_client(authorization)
    now = time.time()
    return [
        {"id": r["id"], "task_id": r["task_id"], "idle_for": round(now - r["seen_at"], 1)}
        for r in q("SELECT * FROM workers ORDER BY seen_at DESC")
    ]


@app.get("/healthz")
async def healthz():
    counts = {
        r["status"]: r["n"]
        for r in q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")
    }
    live = one("SELECT COUNT(*) n FROM workers WHERE seen_at > ?", (time.time() - WORKER_STALE,))
    return {"ok": True, "tasks": counts, "workers": live["n"]}


def main():
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
