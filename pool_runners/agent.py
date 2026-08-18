from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

MARK = "::pool::"
RELEASE = "https://github.com/actions/runner/releases/download/v{version}/actions-runner-linux-{arch}-{version}.tar.gz"
ARCHES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TZ")
TOOLS = "/opt/hostedtoolcache"
LISTENING = "Listening for Jobs"
STARTED = "Running job:"
FINISHED = "completed with result:"
LISTENER = "bin/Runner.Listener"
AGENT = "pool-runners"
LOCK = ".tpl.lock"
WRITE = 0o222
GRACE = 20.0
BUSY_GRACE = 400.0
STALE = 6 * 3600.0
JOIN = 5.0
POLL = 1.0
CHUNK = 1 << 20
ATTEMPTS = 3
SMALL = 1 << 20


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def event(kind: str, **fields: Any) -> None:
    hook = globals().get("emit")
    if callable(hook):
        hook(kind, **fields)
        return
    say(
        MARK
        + json.dumps(
            {"kind": kind, "at": round(time.time(), 3), **fields},
            default=str,
            ensure_ascii=False,
        )
    )


def arch() -> str:
    machine = platform.machine().lower()
    if machine not in ARCHES:
        raise RuntimeError(f"нет раннера под {machine}")
    return ARCHES[machine]


def cache() -> Path:
    chosen = os.environ.get("RUNNERS_CACHE")
    if chosen:
        path = Path(chosen)
        path.mkdir(parents=True, exist_ok=True)
    else:
        home = Path("~").expanduser()
        writable = not str(home).startswith("~") and os.access(home, os.W_OK)
        path = (home / ".cache" if writable else Path(tempfile.gettempdir())) / AGENT
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        kept = path.stat()
        if kept.st_uid != os.getuid():
            raise RuntimeError(f"кэш {path} чужой, uid {kept.st_uid}")
    sweep(path)
    return path


def sweep(path: Path) -> None:
    stale = time.time() - STALE
    for leftover in [*path.glob("runner-*"), *path.glob("tpl-*")]:
        try:
            if leftover.is_dir() and leftover.stat().st_mtime < stale:
                thaw(leftover)
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            continue


def freeze(path: Path) -> None:
    # шаблон общий: любая запись в него из задачи — порча для всех,
    # без права записи это громкий EACCES вместо тихой беды
    for here, folders, files in os.walk(path):
        for name in (*files, *folders):
            spot = Path(here, name)
            with contextlib.suppress(OSError):
                spot.chmod(spot.stat().st_mode & ~WRITE)
    path.chmod(path.stat().st_mode & ~WRITE)


def thaw(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(path.stat().st_mode | 0o200)
    for here, folders, _files in os.walk(path):
        for name in folders:
            spot = Path(here, name)
            with contextlib.suppress(OSError):
                spot.chmod(spot.stat().st_mode | 0o200)


def digest(path: Path) -> str:
    total = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            total.update(block)
    return total.hexdigest()


def download(url: str, target: Path) -> Path:
    part = target.with_name(f"{target.name}.{os.getpid()}.part")
    ask = urllib.request.Request(url, headers={"User-Agent": AGENT})  # noqa: S310
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            answer = urllib.request.urlopen(ask, timeout=120)  # noqa: S310
            with answer, part.open("wb") as out:
                shutil.copyfileobj(answer, out, CHUNK)
                promised = answer.headers.get("Content-Length")
            got = part.stat().st_size
            if got < SMALL:
                raise OSError("подозрительно маленький архив")
            if promised and got != int(promised):
                raise OSError(f"архив оборвался: {got} из {promised} байт")
        except (OSError, ValueError) as exc:
            last = exc
            part.unlink(missing_ok=True)
            if attempt + 1 < ATTEMPTS:
                time.sleep(2**attempt)
        else:
            part.replace(target)
            return target
    raise RuntimeError(f"не скачал раннер: {last}")


def tarball(version: str, sha256: str = "") -> Path:
    path = cache() / f"actions-runner-linux-{arch()}-{version}.tar.gz"
    if not (path.exists() and path.stat().st_size >= SMALL):
        event("runner", state="downloading", version=version)
        download(RELEASE.format(version=version, arch=arch()), path)
    if sha256 and digest(path) != sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 архива не сошёлся с {sha256}")
    return path


def unpack(archive: Path, root: Path) -> Path:
    try:
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(root, filter="data")
            except TypeError:
                tar.extractall(root)  # noqa: S202
        listener = root / LISTENER
        if not listener.exists():
            raise RuntimeError(f"в архиве нет {LISTENER}")
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    listener.chmod(0o755)
    return listener


def template(archive: Path, version: str) -> Path:
    room = cache()
    tpl = room / f"tpl-{arch()}-{version}"
    if (tpl / LISTENER).exists():
        os.utime(tpl)
        return tpl
    with (room / LOCK).open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if (tpl / LISTENER).exists():
            os.utime(tpl)
            return tpl
        if tpl.exists():
            thaw(tpl)
            shutil.rmtree(tpl, ignore_errors=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{tpl.name}.", dir=room))
        try:
            unpack(archive, staging)
            freeze(staging)
            try:
                os.replace(staging, tpl)
            except OSError:
                # гонку выиграл сосед — берём его шаблон
                thaw(staging)
                shutil.rmtree(staging, ignore_errors=True)
        finally:
            if staging.exists():
                thaw(staging)
                shutil.rmtree(staging, ignore_errors=True)
    os.utime(tpl)
    return tpl


def populate(tpl: Path, root: Path) -> Path:
    try:
        shutil.copytree(tpl, root, copy_function=os.link, dirs_exist_ok=True)
    except OSError:
        thaw(root)
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(tpl, root, copy_function=shutil.copy2, dirs_exist_ok=True)
    # файлы — жёсткие ссылки на шаблон и остаются нетронутыми,
    # а каталоги раннеру нужны для _work, _diag, .runner и прочего
    thaw(root)
    listener = root / LISTENER
    mode = listener.stat().st_mode
    if not mode & 0o111:
        listener.chmod(mode | 0o111)
    return listener


def environ(jit: str, work: str, root: Path) -> dict[str, str]:
    clean = {name: os.environ[name] for name in KEEP if os.environ.get(name)}
    clean["ACTIONS_RUNNER_INPUT_JITCONFIG"] = jit
    clean["TMPDIR"] = str(root / "_temp")
    clean.setdefault("HOME", str(root))
    clean.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    if os.getuid() == 0:
        clean["RUNNER_ALLOW_RUNASROOT"] = "1"
    if Path(TOOLS).is_dir():
        clean["AGENT_TOOLSDIRECTORY"] = TOOLS
    Path(clean["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    inside = Path(work)
    (inside if inside.is_absolute() else root / inside).mkdir(
        parents=True, exist_ok=True
    )
    return clean


def kin(root: int) -> list[int]:
    parents: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) > 1:
            parents.setdefault(int(fields[1]), []).append(int(entry.name))
    found: list[int] = []
    stack = [root]
    while stack:
        for child in parents.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def squatters(root: Path) -> list[int]:
    mine = os.getpid()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            where = (entry / "cwd").resolve()
        except OSError:
            continue
        if root == where or root in where.parents:
            found.append(int(entry.name))
    return found


def slay(proc: subprocess.Popen[str], root: Path | None = None) -> None:
    alive = proc.returncode is None
    try:
        tree = kin(proc.pid) if alive else []
    except OSError:
        tree = []
    try:
        stragglers = squatters(root) if root is not None else []
    except OSError:
        stragglers = []
    for pid in [*tree, *stragglers, *([proc.pid] if alive else [])]:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


class State:
    def __init__(self) -> None:
        self.lock: threading.Lock = threading.Lock()
        self.job: str = ""
        self.jobs: int = 0
        self.results: list[str] = []
        self.listening: bool = False
        self.reason: str = ""
        self.born: float = time.monotonic()

    def busy(self) -> bool:
        with self.lock:
            return bool(self.job) or self.jobs > 0

    def read(self, text: str) -> None:
        with self.lock:
            if LISTENING in text and not self.listening:
                self.listening = True
                event("runner", state="listening")
            elif STARTED in text:
                self.job = text.split(STARTED, 1)[1].strip()
                self.jobs += 1
                event("job", state="running", name=self.job)
            elif FINISHED in text and self.job:
                result = text.rsplit(FINISHED, 1)[1].strip()
                self.results.append(result)
                event("job", state="finished", name=self.job, result=result)
                self.job = ""

    def stopping(self, reason: str) -> bool:
        with self.lock:
            if self.reason:
                return False
            self.reason = reason
            return True


def pump(stream: Any, state: State) -> None:
    for raw in iter(stream.readline, ""):
        text = raw.rstrip("\n")
        say(text.replace(MARK, "::pool ::"))
        state.read(text)


def stop(proc: subprocess.Popen[str], state: State, reason: str) -> None:
    if state.stopping(reason):
        event("runner", state="stopping", reason=reason, job=state.job or None)
    with contextlib.suppress(OSError):
        proc.terminate()


def watchdog(
    proc: subprocess.Popen[str],
    state: State,
    idle: float,
    lifetime: float,
    done: threading.Event,
    root: Path,
) -> None:
    while not done.wait(POLL):
        if proc.poll() is not None:
            return
        waited = time.monotonic() - state.born
        overdue = bool(lifetime) and waited > lifetime
        unused = bool(idle) and state.listening and not state.busy() and waited > idle
        if not (overdue or unused):
            continue
        stop(proc, state, "lifetime" if overdue else "idle")
        if not done.wait(BUSY_GRACE if state.busy() else GRACE) and proc.poll() is None:
            event("runner", state="killing")
            slay(proc, root)
        return


def reap(
    proc: subprocess.Popen[str], state: State, root: Path, grace: float = GRACE
) -> int:
    if proc.poll() is not None:
        return proc.returncode
    stop(proc, state, state.reason or "cancel")
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        event("runner", state="killing")
        slay(proc, root)
    try:
        return proc.wait(timeout=GRACE)
    except subprocess.TimeoutExpired:
        return -9


def agent(
    jit: str,
    version: str,
    name: str = "",
    work: str = "_work",
    idle: float = 300.0,
    lifetime: float = 3600.0,
    sha256: str = "",
    slug: str = "",
    label: str = "",
) -> dict[str, Any]:
    archive = tarball(version, sha256)
    tpl = template(archive, version)
    root = Path(tempfile.mkdtemp(prefix="runner-", dir=cache()))
    state = State()
    done = threading.Event()
    proc: subprocess.Popen[str] | None = None
    code = -1

    event(
        "runner", state="starting", name=name, version=version, repo=slug, label=label
    )
    try:
        listener = populate(tpl, root)
        proc = subprocess.Popen(
            [str(listener), "run"],
            cwd=root,
            env=environ(jit, work, root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        watch = threading.Thread(
            target=watchdog,
            args=(proc, state, idle, lifetime, done, root),
            daemon=True,
        )
        watch.start()
        reader = threading.Thread(target=pump, args=(proc.stdout, state), daemon=True)
        reader.start()
        code = proc.wait()
        reader.join(JOIN)
    except TimeoutError:
        state.stopping("lifetime")
    finally:
        done.set()
        if proc is not None:
            code = reap(proc, state, root)
            slay(proc, root)
        shutil.rmtree(root, ignore_errors=True)

    outcome = {
        "runner": name,
        "repo": slug,
        "label": label,
        "jobs": state.jobs,
        "results": state.results,
        "reason": state.reason or "exit",
        "code": code,
        "seconds": round(time.monotonic() - state.born, 1),
    }
    if not state.reason:
        if code:
            raise RuntimeError(f"раннер вышел с кодом {code}: {outcome}")
        if not state.listening:
            raise RuntimeError(f"раннер не встал в очередь: {outcome}")
    event("runner", state="gone", **outcome)
    return outcome
