import asyncio
import contextlib
import os
import signal
import sys
import termios
import tty
from collections.abc import Generator
from typing import Any

import httpx

from gh_pool.status import FINISHED

TIMEOUT = httpx.Timeout(30.0, read=120.0)
MIN_BACKOFF = 0.25
MAX_BACKOFF = 5.0
BITE = 1 << 12


def geometry() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
    except OSError:
        return 80, 24
    return size.columns, size.lines


@contextlib.contextmanager
def raw(fd: int) -> Generator[None]:
    if not os.isatty(fd):
        yield
        return
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class Escape:
    def __init__(self) -> None:
        self.fresh = True
        self.armed = False

    def filter(self, data: bytes) -> tuple[bytes, bool]:
        kept = bytearray()
        for value in data:
            piece = bytes((value,))
            if self.armed:
                self.armed = False
                if piece == b".":
                    return bytes(kept), True
                kept += b"~" + piece
            elif self.fresh and piece == b"~":
                self.armed = True
                continue
            else:
                kept += piece
            self.fresh = piece in (b"\r", b"\n")
        return bytes(kept), False


class Link:
    def __init__(self, client: httpx.AsyncClient, tid: str) -> None:
        self.client = client
        self.tid = tid
        self.read = 0
        self.sent = 0
        self.status = "running"
        self.detached = False
        self.done = asyncio.Event()

    def over(self, status: str) -> None:
        self.status = status
        self.done.set()


async def push_size(link: Link) -> None:
    cols, rows = geometry()
    with contextlib.suppress(Exception):
        await link.client.post(
            f"/v1/shells/{link.tid}/size", json={"cols": cols, "rows": rows}
        )


async def downstream(link: Link) -> None:
    backoff = MIN_BACKOFF
    while not link.done.is_set():
        try:
            answer = await link.client.get(
                f"/v1/shells/{link.tid}/out", params={"offset": link.read}
            )
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        backoff = MIN_BACKOFF
        if answer.status_code >= 400:
            link.over("gone" if answer.status_code == 410 else link.status)
            return
        if answer.content:
            sys.stdout.buffer.write(answer.content)
            sys.stdout.buffer.flush()
        link.read = int(answer.headers.get("X-Shell-Offset", link.read))
        status = answer.headers.get("X-Task-Status", link.status)
        if status in FINISHED or status == "gone":
            link.over(status)
            return


async def upstream(link: Link, queue: asyncio.Queue[bytes]) -> None:
    pending = bytearray()
    while not link.done.is_set():
        pending += await queue.get()
        while not queue.empty():
            pending += queue.get_nowait()
        while pending and not link.done.is_set():
            try:
                answer = await link.client.post(
                    f"/v1/shells/{link.tid}/in",
                    params={"offset": link.sent},
                    content=bytes(pending),
                )
            except Exception:
                await asyncio.sleep(MIN_BACKOFF)
                continue
            if answer.status_code >= 400:
                link.over("gone")
                return
            moved = int(answer.json()["offset"])
            if moved <= link.sent:
                link.over("gone")
                return
            del pending[: min(moved - link.sent, len(pending))]
            link.sent = moved


def wire_stdin(
    loop: asyncio.AbstractEventLoop, link: Link, queue: asyncio.Queue[bytes]
) -> int | None:
    try:
        fd = sys.stdin.fileno()
    except AttributeError, ValueError:
        return None
    escape = Escape()

    def ready() -> None:
        try:
            data = os.read(fd, BITE)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            return
        kept, leaving = escape.filter(data)
        if kept:
            queue.put_nowait(kept)
        if leaving:
            loop.remove_reader(fd)
            link.detached = True
            link.over("detached")

    try:
        loop.add_reader(fd, ready)
    except OSError, NotImplementedError:
        return None
    return fd


def wire_resize(loop: asyncio.AbstractEventLoop, link: Link) -> None:
    def changed() -> None:
        _ = asyncio.ensure_future(push_size(link))  # noqa: RUF006

    with contextlib.suppress(ValueError, NotImplementedError, AttributeError):
        loop.add_signal_handler(signal.SIGWINCH, changed)


async def attach(tid: str, server: str, token: str) -> str:
    async with httpx.AsyncClient(
        base_url=server,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    ) as client:
        link = Link(client, tid)
        opening = await client.get(f"/v1/shells/{tid}")
        if opening.status_code >= 400:
            return "gone"
        link.sent = int(opening.json()["in"])
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        fd = wire_stdin(loop, link, queue)
        wire_resize(loop, link)
        await push_size(link)
        crew = [
            asyncio.create_task(downstream(link)),
            asyncio.create_task(upstream(link, queue)),
        ]
        try:
            await link.done.wait()
        finally:
            if fd is not None:
                with contextlib.suppress(Exception):
                    loop.remove_reader(fd)
            with contextlib.suppress(ValueError, NotImplementedError, AttributeError):
                loop.remove_signal_handler(signal.SIGWINCH)
            for task in crew:
                _ = task.cancel()
            await asyncio.gather(*crew, return_exceptions=True)
        return link.status


def run(tid: str, server: str, token: str) -> str:
    try:
        fd = sys.stdin.fileno()
    except AttributeError, ValueError:
        fd = 0
    with raw(fd):
        status = asyncio.run(attach(tid, server, token))
    sys.stdout.buffer.write(b"\r\n")
    sys.stdout.buffer.flush()
    return status


def payload(command: str | None) -> dict[str, Any]:
    cols, rows = geometry()
    body: dict[str, Any] = {
        "cols": cols,
        "rows": rows,
        "term": os.getenv("TERM") or "xterm-256color",
    }
    if command:
        body["command"] = command
    return body
