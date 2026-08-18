from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from pool_runners.errors import RunnerError

VERSION = "0.1.0"
USER_AGENT = f"pool-runners/{VERSION}"

API_VERSION = "6.0-preview"
REST_VERSION = "2022-11-28"

POLL_TIMEOUT = 90
REQUEST_TIMEOUT = 30
POOL_TIMEOUT = 30

TOKEN_SKEW = 300.0
SESSION_CONFLICT_WAIT = 30.0
SESSION_CONFLICT_TRIES = 10
PREFLIGHT_TTL = 600.0
RELEASE_TTL = 3600.0

MAX_ATTEMPTS = 5
MAX_LOOP_FAILURES = 5
BACKOFF_BASE = 1.5
BACKOFF_CAP = 30.0
RESTART_CAP = 60.0
RESTART_HEALTHY = 300.0

QUEUE_MESSAGE_TYPE = "RunnerScaleSetJobMessages"
JOB_AVAILABLE = "JobAvailable"
JOB_COMPLETED = "JobCompleted"
CAPACITY_HEADER = "X-ScaleSetMaxCapacity"

SUBMIT_WORKERS = 8
FLEET_INTERVAL = 10.0
STATS_INTERVAL = 60.0
WORKER_STALE = 120.0
DRAIN_POLL = 5.0
JOIN_WAIT = 30.0
ALIVE = ("pending", "running")
ADOPT_LIMIT = 1000

RUNNER_NAME_PREFIX = "pool-"
RUNNER_RELEASES = "https://github.com/actions/runner/releases/latest"

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
SESSION_STATUSES = frozenset({401, 404, 409})
RATE_STATUSES = frozenset({403, 429})
RATE_BLIND_WAIT = 60.0
RATE_WAIT_CAP = 300.0
RATE_WINDOW = 3600.0

REPO_PATTERN = re.compile(r"[^/\s]+/[^/\s]+")
UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
TEXT_KEYS = ("token", "label", "work", "version", "sha256")
TIME_KEYS = ("idle", "lifetime", "drain")
KEYS = (*TEXT_KEYS, *TIME_KEYS, "jobs")


def secs(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        if text and text[-1] in UNITS:
            return float(text[:-1]) * UNITS[text[-1]]
        return float(text)
    except ValueError:
        raise RunnerError(f"это не длительность: {value}") from None


def count(value: object, name: str) -> int:
    try:
        number = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        raise RunnerError(f"{name} должен быть числом, получено: {value}") from None
    if number < 1:
        raise RunnerError(f"{name} должен быть больше нуля, получено: {number}")
    return number


@dataclass(frozen=True)
class Target:
    slug: str
    token: str
    label: str = "pool"
    jobs: int = 20
    idle: float = 300.0
    lifetime: float = 3600.0
    drain: float = 60.0
    work: str = "_work"
    version: str = ""
    sha256: str = ""

    def check(self) -> Target:
        if not REPO_PATTERN.fullmatch(self.slug):
            raise RunnerError(f"ожидается owner/name, получено: {self.slug}")
        if not self.token:
            raise RunnerError(f"{self.slug}: нет токена")
        if not self.label:
            raise RunnerError(f"{self.slug}: пустая метка")
        if not self.work.strip():
            raise RunnerError(
                f"{self.slug}: пустой work — раннер уйдёт в вечный перезапуск"
            )
        return self


@dataclass(frozen=True)
class Server:
    url: str
    token: str


def _target(slug: str, raw: dict[str, object]) -> Target:
    fields: dict[str, object] = {}
    for key in KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "jobs":
            fields[key] = count(value, f"{slug}.jobs")
        elif key in TIME_KEYS:
            fields[key] = secs(value)
        else:
            fields[key] = str(value)
    fields.setdefault("token", "")
    return Target(slug=slug, **fields).check()  # pyright: ignore[reportArgumentType]


def load(path: Path) -> tuple[list[Target], Server]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RunnerError(f"не прочитал конфиг {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RunnerError(f"кривой конфиг {path}: {exc}") from None

    repos = raw.pop("repos", {})
    pool = raw.pop("pool", {})
    if not isinstance(repos, dict) or not repos:
        raise RunnerError(f"{path}: не перечислены репозитории в [repos]")

    server = Server(
        url=str(pool.get("server") or env_server().url).rstrip("/"),
        token=str(pool.get("token") or env_server().token),
    )

    targets = []
    for slug, value in repos.items():
        merged = dict(raw)
        merged.update({"token": value} if isinstance(value, str) else dict(value))
        targets.append(_target(str(slug), merged))
    return targets, server


def env_server() -> Server:
    return Server(
        url=os.environ.get("POOL_SERVER", "http://localhost:8000").rstrip("/"),
        token=os.environ.get("POOL_CLIENT_TOKEN", "dev-client"),
    )


def env_target(slug: str) -> Target:
    target = Target(slug=slug, token=os.environ.get("GH_TOKEN", "").strip())
    for key in TEXT_KEYS[1:]:
        value = os.environ.get(f"RUNNERS_{key.upper()}", "").strip()
        if value:
            target = replace(target, **{key: value})
    for key in TIME_KEYS:
        value = os.environ.get(f"RUNNERS_{key.upper()}", "").strip()
        if value:
            target = replace(target, **{key: secs(value)})
    jobs = os.environ.get("RUNNERS_JOBS", "").strip()
    if jobs:
        target = replace(target, jobs=count(jobs, "RUNNERS_JOBS"))
    return target.check()


def api_base() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def server_url() -> str:
    return os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")


def debug() -> bool:
    return bool(os.environ.get("RUNNERS_DEBUG"))
