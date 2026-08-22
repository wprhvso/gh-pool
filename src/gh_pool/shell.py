import contextlib
import fcntl
import json
import os
import pty
import signal
import struct
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, NoReturn

from gh_pool import rpc

SERVER = os.getenv("GH_POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("GH_POOL_WORKER_TOKEN", "dev-worker")
TASK = os.getenv("GH_POOL_TASK", "")
CHUNK = 1 << 16
TIMEOUT = 90.0
MAX_BACKOFF = 15.0
DRAIN = 5.0
GRACE = 2.0


class Gone(Exception):
    pass


def note(message: str) -> None:
    sys.stderr.write(f"[shell] {message}\n")
    sys.stderr.flush()


def call(method: str, path: str, data: bytes | None = None) -> tuple[Any, bytes]:
    delay = 0.5
    while True:
        request = urllib.request.Request(  # noqa: S310
            SERVER + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:  # noqa: S310
                return answer.headers, answer.read()
        except urllib.error.HTTPError as refusal:
            if refusal.code < 500 and refusal.code != 429:
                raise Gone(f"{refusal.code} {refusal.reason}") from None
        except Exception as broken:
            note(f"{type(broken).__name__}, ещё раз через {delay:.1f}с")
        time.sleep(delay)
        delay = min(delay * 2, MAX_BACKOFF)


def push(path: str, sent: int, pending: bytearray) -> int:
    while pending:
        _, body = call("POST", f"{path}?offset={sent}", bytes(pending))
        moved = int(json.loads(body)["offset"])
        if moved <= sent:
            raise Gone("канал сбросили под нами")
        del pending[: moved - sent]
        sent = moved
    return sent


def resize(master: int, geometry: str) -> None:
    cols, _, rows = geometry.partition("x")
    with contextlib.suppress(ValueError, OSError):
        fcntl.ioctl(
            master, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0)
        )


def pump_out(master: int, stop: threading.Event) -> None:
    sent = 0
    pending = bytearray()
    while not stop.is_set():
        try:
            data = os.read(master, CHUNK)
        except OSError:
            return
        if not data:
            return
        pending += data
        sent = push(f"/v1/shells/{TASK}/out", sent, pending)


def pump_in(master: int, stop: threading.Event) -> None:
    got = 0
    geometry = ""
    while not stop.is_set():
        headers, body = call("GET", f"/v1/shells/{TASK}/in?offset={got}")
        got = int(headers.get("X-Shell-Offset", got))
        if body:
            os.write(master, body)
        current = headers.get("X-Shell-Size", "")
        if current and current != geometry:
            geometry = current
            resize(master, current)


def guard(
    pump: Callable[[int, threading.Event], None], master: int, stop: threading.Event
) -> None:
    try:
        pump(master, stop)
    except Gone as closed:
        note(f"канал закрыт: {closed}")
    except Exception as broken:
        note(f"{type(broken).__name__}: {broken}")
    finally:
        stop.set()


def spawn(
    pump: Callable[[int, threading.Event], None], master: int, stop: threading.Event
) -> threading.Thread:
    thread = threading.Thread(target=guard, args=(pump, master, stop), daemon=True)
    thread.start()
    return thread


def reap(pid: int) -> int | None:
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0
    if not done:
        return None
    try:
        return os.waitstatus_to_exitcode(status)
    except ValueError:
        return None


def wait(pid: int, stop: threading.Event) -> int | None:
    while not stop.is_set():
        code = reap(pid)
        if code is not None:
            return code
        time.sleep(0.1)
    return None


def kill(pid: int) -> None:
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, ChildProcessError):
            return
        deadline = time.monotonic() + GRACE
        while time.monotonic() < deadline:
            if reap(pid) is not None:
                return
            time.sleep(0.05)


def argv(payload: dict[str, Any]) -> list[str]:
    command = payload.get("command")
    if isinstance(command, str):
        return ["/bin/sh", "-c", command]
    if command:
        return [str(piece) for piece in command]
    return [os.environ.get("SHELL") or "/bin/sh", "-i"]


def child(line: list[str], payload: dict[str, Any]) -> NoReturn:
    environment = dict(os.environ)
    environment["TERM"] = str(payload.get("term") or "xterm-256color")
    environment["GH_POOL_SHELL"] = TASK
    environment.setdefault("HOME", "/root")
    with contextlib.suppress(OSError):
        os.chdir(str(payload.get("cwd") or environment.get("HOME") or "/"))
    try:
        os.execvpe(line[0], line, environment)  # noqa: S606
    except OSError as missing:
        sys.stderr.write(f"{line[0]}: {missing}\n")
    os._exit(127)


def shell(payload: dict[str, Any]) -> None:
    if not TASK:
        raise RuntimeError("GH_POOL_TASK не задан, обслуживать нечего")
    line = argv(payload)
    cols = int(payload.get("cols") or 80)
    rows = int(payload.get("rows") or 24)
    pid, master = pty.fork()
    if pid == 0:
        child(line, payload)
    resize(master, f"{cols}x{rows}")
    stop = threading.Event()
    out = spawn(pump_out, master, stop)
    spawn(pump_in, master, stop)
    rpc.emit("shell", command=line, cols=cols, rows=rows)
    try:
        code = wait(pid, stop)
    finally:
        kill(pid)
    out.join(DRAIN)
    stop.set()
    rpc.emit("result", {"exit": code})
