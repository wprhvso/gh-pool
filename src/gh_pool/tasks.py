import hashlib
import json
import linecache
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

from gh_pool import rpc

DEPS = Path(os.getenv("POOL_DEPS", "/tmp/pool-deps"))


def install(deps: list[str]) -> None:
    target = DEPS / hashlib.sha256("\n".join(sorted(deps)).encode()).hexdigest()[:16]
    if not (target / ".ok").exists():
        print(f"[deps] {' '.join(deps)}")  # noqa: T201
        uv = shutil.which("uv")
        base = (
            [uv, "pip", "install", "--python", sys.executable]
            if uv
            else [sys.executable, "-m", "pip", "install"]
        )
        subprocess.run(
            [*base, "--target", str(target), *deps],
            check=True,
            stdout=sys.stdout,
            stderr=subprocess.STDOUT,
        )
        (target / ".ok").touch()
    sys.path.insert(0, str(target))


def expired(*_: Any) -> NoReturn:
    raise TimeoutError("task timed out")


def run(payload: dict[str, Any]) -> None:
    code = payload["code"]
    entry = payload.get("entry")
    args = payload.get("args") or []
    kwargs = payload.get("kwargs") or {}

    if payload.get("deps"):
        install(payload["deps"])
    if payload.get("timeout"):
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, float(payload["timeout"]))

    name = f"<{entry or 'code'}>"
    linecache.cache[name] = (len(code), None, code.splitlines(True), name)
    scope: dict[str, Any] = {
        "__name__": "__pool__",
        "args": args,
        "kwargs": kwargs,
        "emit": rpc.emit,
    }
    try:
        exec(compile(code, name, "exec"), scope)  # noqa: S102
        if entry and not callable(scope.get(entry)):
            raise NameError(f"{entry} is not defined by the submitted code")
        value = scope[entry](*args, **kwargs) if entry else scope.get("result")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)

    if value is None:
        return
    try:
        json.dumps(value)
    except TypeError as e:
        raise TypeError(f"returned value must be JSON: {e}") from None
    rpc.emit("result", value)


def python(payload: dict[str, Any]) -> None:
    try:
        run(payload)
    except Exception as e:
        rpc.emit("error", type=type(e).__name__, message=str(e))
        raise


REGISTRY = {"python": python}
