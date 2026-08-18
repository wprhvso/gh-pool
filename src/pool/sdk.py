from __future__ import annotations

import inspect
import os
import textwrap
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import IO, Any, Self

import httpx

from pool import rpc

TYPE = "python"
TERMINAL = ("done", "failed", "cancelled", "lost")


class Failed(RuntimeError):
    def __init__(self, tid: str, status: str, error: str | None, tail: str) -> None:
        event = next(
            (e for e in reversed(rpc.parse(tail)) if e.get("kind") == "error"), None
        )
        self.event = event
        detail = event and (event.get("message") or event.get("value"))
        if event and detail:
            error = f"{event.get('type', 'error')}: {detail}"
        super().__init__(
            f"task {tid} {status}"
            + (f": {error}" if error else "")
            + (f"\n{tail}" if tail else "")
        )
        self.task_id = tid
        self.status = status
        self.error = error
        self.tail = tail


def source(fn: Callable[..., Any]) -> str:
    try:
        lines = textwrap.dedent(inspect.getsource(fn)).splitlines()
    except (OSError, TypeError):
        raise TypeError(f"no source for {fn!r}, pass the code as a string") from None
    start = next((i for i, line in enumerate(lines) if line.startswith("def ")), None)
    if start is None:
        raise TypeError(f"{fn!r} is not a plain function")
    return "\n".join(lines[start:])


class Task:
    def __init__(self, pool: Pool, tid: str) -> None:
        self.pool = pool
        self.id = tid

    def __repr__(self) -> str:
        return f"<Task {self.id}>"

    def state(self) -> dict[str, Any]:
        return self.pool.call("GET", f"/v1/tasks/{self.id}").json()

    def wait(self, poll: float = 0.25) -> dict[str, Any]:
        while True:
            state = self.state()
            if state["status"] in TERMINAL:
                return state
            time.sleep(poll)
            poll = min(poll * 1.5, 5.0)

    def check(self) -> Self:
        state = self.wait()
        if state["status"] != "done":
            raise Failed(self.id, state["status"], state.get("error"), self.tail())
        return self

    def raw(self, offset: int = 0) -> bytes:
        return self.pool.call(
            "GET", f"/v1/tasks/{self.id}/events", params={"offset": offset}
        ).content

    def tail(self, limit: int = 4000) -> str:
        head = self.pool.call(
            "GET", f"/v1/tasks/{self.id}/events", params={"offset": 1 << 40}
        )
        size = int(head.headers["X-Event-Size"])
        return self.raw(max(size - limit, 0)).decode(errors="replace")

    def events(self, offset: int = 0) -> list[dict[str, Any]]:
        return rpc.parse(self.raw(offset).decode(errors="replace"))

    def watch(self, offset: int = 0) -> Iterator[dict[str, Any]]:
        buf = b""
        for chunk in self.follow(offset):
            buf += chunk
            head, _, buf = buf.rpartition(b"\n")
            yield from rpc.parse(head.decode(errors="replace"))

    def follow(self, offset: int = 0) -> Iterator[bytes]:
        while True:
            r = self.pool.call(
                "GET", f"/v1/tasks/{self.id}/events", params={"offset": offset}
            )
            if r.content:
                yield r.content
            offset = int(r.headers["X-Event-Offset"])
            if r.headers["X-Task-Status"] in TERMINAL and offset >= int(
                r.headers["X-Event-Size"]
            ):
                return
            time.sleep(0.5)

    def cancel(self) -> dict[str, Any]:
        return self.pool.call("POST", f"/v1/tasks/{self.id}/cancel").json()


class Remote:
    def __init__(
        self,
        pool: Pool,
        fn: Callable[..., Any] | str,
        deps: Iterable[str] = (),
        timeout: float | None = None,
    ) -> None:
        self.pool = pool
        self.fn = fn if callable(fn) else None
        self.code = source(fn) if callable(fn) else fn
        self.entry = fn.__name__ if callable(fn) else None
        self.deps = list(deps)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"<Remote {self.entry or 'code'}>"

    def __call__(self, *args: Any, **kwargs: Any) -> Task:
        return self.submit(*args, **kwargs).check()

    def submit(self, *args: Any, **kwargs: Any) -> Task:
        payload: dict[str, Any] = {
            "code": self.code,
            "args": list(args),
            "kwargs": kwargs,
        }
        if self.entry:
            payload["entry"] = self.entry
        if self.deps:
            payload["deps"] = self.deps
        if self.timeout:
            payload["timeout"] = self.timeout
        body = self.pool.call(
            "POST", "/v1/tasks", json={"type": TYPE, "payload": payload}
        ).json()
        return Task(self.pool, body["task_id"])

    def spawn(self, items: Iterable[Any]) -> list[Task]:
        return [
            self.submit(*x) if isinstance(x, tuple) else self.submit(x) for x in items
        ]

    def map(self, items: Iterable[Any]) -> list[Task]:
        return [t.check() for t in self.spawn(items)]


class Pool:
    def __init__(
        self,
        server: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.http = httpx.Client(
            base_url=(
                server or os.getenv("POOL_SERVER", "http://localhost:8000")
            ).rstrip("/"),
            headers={
                "Authorization": f"Bearer {token or os.getenv('POOL_CLIENT_TOKEN', 'dev-client')}"
            },
            timeout=httpx.Timeout(timeout, read=300.0),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
        return r

    def remote(
        self,
        fn: Callable[..., Any] | str | None = None,
        *,
        deps: Iterable[str] = (),
        timeout: float | None = None,
    ) -> Remote | Callable[[Callable[..., Any]], Remote]:
        if fn is None:
            return lambda f: Remote(self, f, deps, timeout)
        return Remote(self, fn, deps, timeout)

    def submit(self, fn: Callable[..., Any] | str, *args: Any, **kwargs: Any) -> Task:
        return Remote(self, fn).submit(*args, **kwargs)

    def run(self, fn: Callable[..., Any] | str, *args: Any, **kwargs: Any) -> Task:
        return Remote(self, fn)(*args, **kwargs)

    def map(self, fn: Callable[..., Any] | str, items: Iterable[Any]) -> list[Task]:
        return Remote(self, fn).map(items)

    def put(
        self,
        key: str,
        data: Path | str | bytes | IO[bytes],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        params = {"task_id": task_id} if task_id else None
        if isinstance(data, Path):
            with data.open("rb") as f:
                return self.call(
                    "PUT", f"/v1/artifacts/{key}", content=f, params=params
                ).json()
        return self.call(
            "PUT", f"/v1/artifacts/{key}", content=data, params=params
        ).json()

    def get(self, key: str) -> bytes:
        return self.call("GET", f"/v1/artifacts/{key}").content

    def download(self, key: str, path: str | Path) -> str | Path:
        with self.http.stream("GET", f"/v1/artifacts/{key}") as r:
            if r.status_code >= 400:
                r.read()
                raise RuntimeError(f"GET {key} -> {r.status_code} {r.text[:200]}")
            with Path(path).open("wb") as f:
                f.writelines(r.iter_bytes())
        return path

    def delete(self, key: str) -> dict[str, Any]:
        return self.call("DELETE", f"/v1/artifacts/{key}").json()

    def artifacts(self, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return self.call(
            "GET", "/v1/artifacts", params={"prefix": prefix, "limit": limit}
        ).json()

    def task(self, tid: str) -> Task:
        return Task(self, tid)

    def tasks(self, status: str | None = None, limit: int = 30) -> list[Task]:
        params = {"limit": limit} | ({"status": status} if status else {})
        return [
            Task(self, t["id"])
            for t in self.call("GET", "/v1/tasks", params=params).json()
        ]

    def health(self) -> dict[str, Any]:
        return self.call("GET", "/healthz").json()
