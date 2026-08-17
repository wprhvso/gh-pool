import inspect
import os
import textwrap
import time
from pathlib import Path

import httpx

from pool import rpc

TYPE = "python"
TERMINAL = ("done", "failed", "cancelled", "lost")


class Failed(RuntimeError):
    def __init__(self, tid, status, error, tail):
        self.event = next((e for e in reversed(rpc.parse(tail)) if e.get("kind") == "error"), None)
        detail = self.event and (self.event.get("message") or self.event.get("value"))
        if detail:
            error = f"{self.event.get('type', 'error')}: {detail}"
        super().__init__(f"task {tid} {status}" + (f": {error}" if error else "") + (f"\n{tail}" if tail else ""))
        self.task_id = tid
        self.status = status
        self.error = error
        self.tail = tail


def source(fn):
    try:
        lines = textwrap.dedent(inspect.getsource(fn)).splitlines()
    except (OSError, TypeError):
        raise TypeError(f"no source for {fn!r}, pass the code as a string") from None
    start = next((i for i, line in enumerate(lines) if line.startswith("def ")), None)
    if start is None:
        raise TypeError(f"{fn!r} is not a plain function")
    return "\n".join(lines[start:])


class Task:
    def __init__(self, pool, tid):
        self.pool = pool
        self.id = tid

    def __repr__(self):
        return f"<Task {self.id}>"

    def state(self):
        return self.pool.call("GET", f"/v1/tasks/{self.id}").json()

    def wait(self, poll=0.25):
        while True:
            state = self.state()
            if state["status"] in TERMINAL:
                return state
            time.sleep(poll)
            poll = min(poll * 1.5, 5.0)

    def check(self):
        state = self.wait()
        if state["status"] != "done":
            raise Failed(self.id, state["status"], state.get("error"), self.tail())
        return self

    def raw(self, offset=0):
        return self.pool.call("GET", f"/v1/tasks/{self.id}/events", params={"offset": offset}).content

    def tail(self, limit=4000):
        head = self.pool.call("GET", f"/v1/tasks/{self.id}/events", params={"offset": 1 << 40})
        size = int(head.headers["X-Event-Size"])
        return self.raw(max(size - limit, 0)).decode(errors="replace")

    def events(self, offset=0):
        return rpc.parse(self.raw(offset).decode(errors="replace"))

    def watch(self, offset=0):
        buf = b""
        for chunk in self.follow(offset):
            buf += chunk
            head, _, buf = buf.rpartition(b"\n")
            yield from rpc.parse(head.decode(errors="replace"))

    def follow(self, offset=0):
        while True:
            r = self.pool.call("GET", f"/v1/tasks/{self.id}/events", params={"offset": offset})
            if r.content:
                yield r.content
            offset = int(r.headers["X-Event-Offset"])
            if r.headers["X-Task-Status"] in TERMINAL and offset >= int(r.headers["X-Event-Size"]):
                return
            time.sleep(0.5)

    def cancel(self):
        return self.pool.call("POST", f"/v1/tasks/{self.id}/cancel").json()


class Remote:
    def __init__(self, pool, fn, deps=(), timeout=None):
        self.pool = pool
        self.fn = fn if callable(fn) else None
        self.code = source(fn) if callable(fn) else fn
        self.entry = fn.__name__ if callable(fn) else None
        self.deps = list(deps)
        self.timeout = timeout

    def __repr__(self):
        return f"<Remote {self.entry or 'code'}>"

    def __call__(self, *args, **kwargs):
        return self.submit(*args, **kwargs).check()

    def submit(self, *args, **kwargs):
        payload = {"code": self.code, "args": list(args), "kwargs": kwargs}
        if self.entry:
            payload["entry"] = self.entry
        if self.deps:
            payload["deps"] = self.deps
        if self.timeout:
            payload["timeout"] = self.timeout
        body = self.pool.call("POST", "/v1/tasks", json={"type": TYPE, "payload": payload}).json()
        return Task(self.pool, body["task_id"])

    def spawn(self, items):
        return [self.submit(*x) if isinstance(x, tuple) else self.submit(x) for x in items]

    def map(self, items):
        return [t.check() for t in self.spawn(items)]


class Pool:
    def __init__(self, server=None, token=None, timeout=30.0):
        self.http = httpx.Client(
            base_url=(server or os.getenv("POOL_SERVER", "http://localhost:8000")).rstrip("/"),
            headers={"Authorization": f"Bearer {token or os.getenv('POOL_CLIENT_TOKEN', 'dev-client')}"},
            timeout=httpx.Timeout(timeout, read=300.0),
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.http.close()

    def call(self, method, path, **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
        return r

    def remote(self, fn=None, *, deps=(), timeout=None):
        if fn is None:
            return lambda f: Remote(self, f, deps, timeout)
        return Remote(self, fn, deps, timeout)

    def submit(self, fn, *args, **kwargs):
        return Remote(self, fn).submit(*args, **kwargs)

    def run(self, fn, *args, **kwargs):
        return Remote(self, fn)(*args, **kwargs)

    def map(self, fn, items):
        return Remote(self, fn).map(items)

    def put(self, key, data, task_id=None):
        params = {"task_id": task_id} if task_id else None
        if isinstance(data, Path):
            with data.open("rb") as f:
                return self.call("PUT", f"/v1/artifacts/{key}", content=f, params=params).json()
        return self.call("PUT", f"/v1/artifacts/{key}", content=data, params=params).json()

    def get(self, key):
        return self.call("GET", f"/v1/artifacts/{key}").content

    def download(self, key, path):
        with self.http.stream("GET", f"/v1/artifacts/{key}") as r:
            if r.status_code >= 400:
                r.read()
                raise RuntimeError(f"GET {key} -> {r.status_code} {r.text[:200]}")
            with open(path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return path

    def delete(self, key):
        return self.call("DELETE", f"/v1/artifacts/{key}").json()

    def artifacts(self, prefix="", limit=100):
        return self.call("GET", "/v1/artifacts", params={"prefix": prefix, "limit": limit}).json()

    def task(self, tid):
        return Task(self, tid)

    def tasks(self, status=None, limit=30):
        params = {"limit": limit} | ({"status": status} if status else {})
        return [Task(self, t["id"]) for t in self.call("GET", "/v1/tasks", params=params).json()]

    def health(self):
        return self.call("GET", "/healthz").json()
