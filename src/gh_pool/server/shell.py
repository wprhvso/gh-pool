import asyncio
import contextlib
import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from gh_pool.core.config import settings
from gh_pool.server.tasks import TASKS, auth_client, auth_worker
from gh_pool.status import FINISHED

router = APIRouter(prefix="/v1/shells", tags=["shell"])

SHELLS: dict[str, Shell] = {}


class Size(BaseModel):
    cols: int = Field(ge=1, le=10000)
    rows: int = Field(ge=1, le=10000)


class Stream:
    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._buf = bytearray()
        self.base = 0
        self.event = asyncio.Event()

    @property
    def size(self) -> int:
        return self.base + len(self._buf)

    def append(self, data: bytes) -> None:
        self._buf += data
        if (over := len(self._buf) - self._cap) > 0:
            del self._buf[:over]
            self.base += over
        self.event.set()

    def read(self, offset: int) -> tuple[int, bytes]:
        start = min(max(offset, self.base), self.size)
        return start, bytes(self._buf[start - self.base :])

    async def wait(self, offset: int, timeout: float) -> None:
        if offset < self.size:
            return
        self.event.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self.event.wait()


class Shell:
    def __init__(self) -> None:
        self.out = Stream(settings.shell_cap)
        self.inp = Stream(settings.shell_cap)
        self.cols = 80
        self.rows = 24
        self.seen_at = time.time()

    @property
    def geometry(self) -> str:
        return f"{self.cols}x{self.rows}"

    @property
    def idle(self) -> float:
        return time.time() - self.seen_at

    def resize(self, cols: int, rows: int) -> None:
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self.inp.event.set()


def _sweep() -> None:
    for tid in [
        s for s in SHELLS if (t := TASKS.get(s)) is None or t["status"] in FINISHED
    ]:
        SHELLS.pop(tid, None)


def _shell(tid: str) -> Shell:
    _sweep()
    task = TASKS.get(tid)
    if task is None or task["status"] in FINISHED:
        raise HTTPException(410, "no such live shell")
    if task["type"] != "shell":
        raise HTTPException(409, "not a shell task")
    return SHELLS.setdefault(tid, Shell())


def _advance(stream: Stream, offset: int, data: bytes) -> dict[str, int]:
    if offset == stream.size:
        stream.append(data)
    return {"offset": stream.size}


def _served(stream: Stream, offset: int, extra: dict[str, str]) -> Response:
    start, data = stream.read(offset)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Shell-Offset": str(start + len(data)), **extra},
    )


@router.get("/{tid}")
async def describe(
    tid: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    auth_client(authorization)
    shell = _shell(tid)
    shell.seen_at = time.time()
    return {
        "out": shell.out.size,
        "in": shell.inp.size,
        "cols": shell.cols,
        "rows": shell.rows,
    }


@router.post("/{tid}/out")
async def push_out(
    tid: str,
    request: Request,
    offset: Annotated[int, Query()],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    auth_worker(authorization)
    return _advance(_shell(tid).out, offset, await request.body())


@router.get("/{tid}/out")
async def pull_out(
    tid: str,
    offset: Annotated[int, Query()] = 0,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    auth_client(authorization)
    shell = _shell(tid)
    shell.seen_at = time.time()
    await shell.out.wait(offset, settings.shell_poll)
    shell.seen_at = time.time()
    task: dict[str, Any] = TASKS.get(tid) or {}
    return _served(shell.out, offset, {"X-Task-Status": task.get("status") or "gone"})


@router.post("/{tid}/in")
async def push_in(
    tid: str,
    request: Request,
    offset: Annotated[int, Query()],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    auth_client(authorization)
    shell = _shell(tid)
    shell.seen_at = time.time()
    return _advance(shell.inp, offset, await request.body())


@router.get("/{tid}/in")
async def pull_in(
    tid: str,
    offset: Annotated[int, Query()] = 0,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    auth_worker(authorization)
    shell = _shell(tid)
    if shell.idle > settings.shell_idle:
        raise HTTPException(410, "nobody is attached")
    await shell.inp.wait(offset, settings.shell_poll)
    return _served(shell.inp, offset, {"X-Shell-Size": shell.geometry})


@router.post("/{tid}/size", status_code=204)
async def set_size(
    tid: str,
    size: Size,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    auth_client(authorization)
    shell = _shell(tid)
    shell.seen_at = time.time()
    shell.resize(size.cols, size.rows)
