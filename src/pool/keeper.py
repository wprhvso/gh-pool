import argparse
import base64
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Final

import structlog
from opentelemetry.metrics import get_meter
from yaol import setup, shutdown, span

from pool.obs import observability

API = "https://api.github.com"
KEYS = ("token", "workflow", "jobs", "ttl", "ref")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SECRETS = ("POOL_SERVER", "POOL_TOKEN")
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# Окно между dispatch и появлением прогона в списке — только на видимость API.
GRACE = 30
# Пока прогон моложе этого, он считается прогревающимся; старше и всё ещё не
# обслуживает — зомби, и место под него надо освободить, а не ждать шесть часов.
# Замер по живому флоту: медиана 212 с, p90 888 с, максимум 922 с. Прежние 240 с
# были взяты по одному наблюдению и резали всё выше медианы: 17 отмен из 21
# пришлись на здоровые прогоны возрастом 247-443 с, и флот не набирался.
# Настоящие зависания ловит не этот таймер, а границы на шагах.
BOOT_GRACE = 1200
# Сколько прогонов keeper вправе погасить за один тик. Массовая отмена — это
# массовая потеря задач: пул считает их lost только через LOST_AFTER и не
# перезапускает, а CI-джобы уходят на второй круг.
RETIRE_BUDGET = 1
# Прогоны без машины гасить безопасно, но не сотнями за тик: на разборе
# накопившейся очереди это упирается в лимит запросов GitHub.
IDLE_BUDGET = 25
# Сколько поднимать за тик, чтобы холодный старт не шёл стадом.
MAX_LAUNCH = 5
# WORKERS на сервере в памяти: сразу после его рестарта он пуст. Столько ждём,
# прежде чем верить нулям (2 × WORKER_STALE сервера).
POOL_WARMUP = 240
# Незавершённые статусы, которые спрашиваем поимённо. Списком «всех прогонов»
# пользоваться нельзя: он отдаёт одну страницу новыми вперёд, и на истории в
# тысячу прогонов долгоживущие in_progress с неё просто уезжают — флот
# становится невидимым, а место занимают свежие queued.
STATUSES = ("in_progress", "queued", "waiting")


class ApiError(RuntimeError):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(text)
        self.code = code


def gh(
    token: str, path: str, method: str = "GET", body: dict[str, Any] | None = None
) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310
        API + path,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "pool-keeper",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            data = r.read()
    except urllib.error.HTTPError as e:
        raise ApiError(
            e.code, f"{method} {path} -> {e.code} {e.read()[:200].decode()}"
        ) from None
    return json.loads(data) if data else {}


def secs(v: str | float) -> float:
    return float(v) if isinstance(v, (int, float)) else float(v[:-1]) * UNITS[v[-1]]


_log: Final = structlog.get_logger("pool.keeper")

_meter: Final = get_meter("pool.keeper")
_launched: Final = _meter.create_counter(
    "pool.keeper.runs.launched",
    unit="1",
    description="Workflow runs dispatched by the keeper",
)
_retired: Final = _meter.create_counter(
    "pool.keeper.runs.retired",
    unit="1",
    description="Workflow runs cancelled by the keeper",
)
_serving: Final = _meter.create_gauge(
    "pool.keeper.workers.serving",
    unit="1",
    description="Workers the pool reports as leasing tasks",
)


def log(msg: str) -> None:
    _log.info(msg)


def age(run: dict[str, Any]) -> float:
    return time.time() - datetime.fromisoformat(run["created_at"]).timestamp()


@dataclass
class Repo:
    slug: str
    token: str
    workflow: str = "pool.yml"
    jobs: int = 20
    ttl: str | float = "6h"
    ref: str = ""
    recent: list[float] = field(default_factory=list)
    stopping: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.ttl = secs(self.ttl)

    def api(
        self, path: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return gh(self.token, f"/repos/{self.slug}{path}", method, body)

    def get(self, path: str) -> dict[str, Any] | None:
        try:
            return self.api(path)
        except ApiError as e:
            if e.code == 404:
                return None
            raise


def load(path: Path) -> tuple[list[Repo], float, dict[str, str], str]:
    raw = tomllib.loads(path.read_text())
    repos = raw.pop("repos", {})
    # token уезжает в секреты репозиториев, им авторизуются воркеры. Спрашивать
    # им /v1/workers нельзя, это клиентская ручка, поэтому client_token отдельно
    # и из propagation исключён.
    table = raw.pop("pool", {})
    client = str(table.pop("client_token", ""))
    pool = {f"POOL_{k.upper()}": str(v) for k, v in table.items()}
    out = []
    for slug, value in repos.items():
        cfg: dict[str, Any] = {
            **raw,
            **({"token": value} if isinstance(value, str) else value),
        }
        if "token" not in cfg:
            sys.exit(f"{slug}: no token")
        out.append(Repo(slug, **{k: cfg[k] for k in KEYS if k in cfg}))
    if not out:
        sys.exit(f"{path}: no repos")
    return out, secs(raw.get("poll", 60)), pool, client


def ask(base: str, path: str, token: str) -> Any:
    req = urllib.request.Request(  # noqa: S310
        f"{base.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # HTTPError — подкласс OSError, и без этой ветки 401 от неверного токена
        # выглядел бы как «пул не отвечает», то есть как временная сетевая беда.
        raise ApiError(e.code, f"GET {path} -> {e.code}") from None


def pool_workers(pool: dict[str, str], token: str) -> dict[int, float] | None:
    """Прогон -> сколько секунд он обслуживает. None — судить нельзя.

    Единственный честный источник: у GitHub в объекте прогона нет момента
    получения машины (run_started_at — это тоже постановка в очередь), а
    WORKER_ID содержит id прогона, и в WORKERS воркер попадает только дойдя
    до pool-worker.
    """
    base = (pool.get("POOL_SERVER") or "").strip()
    if not base:
        return None
    if not token:
        log("в конфиге нет pool.client_token, воркеров не видно — ttl не работает")
        return None
    try:
        up = time.time() - float(ask(base, "/healthz", token).get("started_at") or 0)
        if up < POOL_WARMUP:
            log(f"pool поднялся {up:.0f}с назад, список воркеров ещё не наполнился")
            return None
        listing = ask(base, "/v1/workers", token)
    except ApiError as e:
        log(f"pool отказал: {e} — проверь pool.client_token")
        return None
    except (OSError, ValueError) as e:
        log(f"pool не отвечает: {e}")
        return None
    out: dict[int, float] = {}
    for w in listing:
        tail = str(w.get("id", "")).rpartition("-")[2]
        if tail.isdigit():
            out[int(tail)] = float(w.get("serving_for") or 0)
    return out


def reconcile(r: Repo, workers: dict[int, float] | None = None) -> None:
    with span("pool.keeper.reconcile", {"repo": r.slug}):
        _reconcile(r, workers)


def unfinished(r: Repo) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for status in STATUSES:
        answer = r.api(
            f"/actions/workflows/{r.workflow}/runs?status={status}&per_page=100"
        )
        for x in answer["workflow_runs"]:
            found[x["id"]] = x
    return sorted(found.values(), key=lambda x: x["created_at"], reverse=True)


def _reconcile(r: Repo, workers: dict[int, float] | None = None) -> None:
    r.ref = r.ref or r.api("")["default_branch"]
    runs = unfinished(r)
    r.stopping &= {x["id"] for x in runs}

    ttl = secs(r.ttl)
    started, pending, expired = [], [], []
    for run in runs:
        if run["status"] != "in_progress":
            # Машину не дали: тут created_at и есть время ожидания.
            (expired if age(run) > ttl else pending).append(run)
        elif workers is None:
            # Судить по created_at нельзя: прогон мог сутки простоять в
            # очереди, и его возраст ничего не говорит о возрасте воркера.
            # Значит, пока пул молчит, занятых не трогаем вовсе — сверху их
            # всё равно ограничивают timeout-minutes и WORKER_MAX_AGE.
            started.append(run)
        elif run["id"] in workers:
            (expired if workers[run["id"]] > ttl else started).append(run)
        elif age(run) > BOOT_GRACE:
            # Прогретый, но так и не начавший обслуживать — зомби, место
            # под него надо освободить, а не ждать до самого ttl.
            expired.append(run)
        else:
            started.append(run)
    serving = None if workers is None else len(set(workers) & {x["id"] for x in runs})

    # Лишние срезаем с молодого конца: старый прогон почти наверняка держит
    # CI-джобу, и его отмена роняет задачу в lost (терминально) вместе с полным
    # бутстрапом на второй круг. Сначала уходят те, кому машину ещё не дали —
    # они не обслуживают ничего, и терять с ними нечего.
    over = max(0, len(started) + len(pending) - r.jobs)
    surplus = pending[:over] + started[: max(0, over - len(pending))]
    victims = [(run, "expired") for run in expired] + [
        (run, "surplus") for run in surplus
    ]
    victims.sort(key=lambda v: v[0]["status"] == "in_progress")
    stopped = costly = 0
    for run, why in victims:
        if run["id"] in r.stopping:
            continue
        busy = run["status"] == "in_progress"
        used, limit = (
            (costly, RETIRE_BUDGET) if busy else (stopped - costly, IDLE_BUDGET)
        )
        if used >= limit:
            log(f"{r.slug} {len(victims) - stopped} more to retire, next tick")
            break
        r.stopping.add(run["id"])
        r.api(f"/actions/runs/{run['id']}/cancel", "POST")
        stopped += 1
        costly += busy
        log(
            f"{r.slug} stopping run {run['id']} reason={why} "
            f"status={run['status']} age={age(run):.0f}s"
        )

    now = time.time()
    r.recent = [t for t in r.recent if now - t < GRACE]
    # started уже без зомби и без просроченных, так что это и есть ёмкость.
    # Прогон без машины не обслуживает ничего, но досылать поверх него нечего:
    # он уже стоит в очереди GitHub и рано или поздно станет воркером.
    young = [x for x in started if age(x) < BOOT_GRACE]
    live = min(len(started) + len(pending), r.jobs) + len(r.recent)
    launch = min(max(0, r.jobs - live), MAX_LAUNCH)
    for _ in range(launch):
        r.api(f"/actions/workflows/{r.workflow}/dispatches", "POST", {"ref": r.ref})
        r.recent.append(now)
    if serving is not None:
        _serving.set(serving, {"repo": r.slug})
    if launch:
        _launched.add(launch, {"repo": r.slug})
    if stopped:
        _retired.add(stopped, {"repo": r.slug})
    _log.info(
        "tick",
        repo=r.slug,
        serving=serving,
        young=len(young),
        started=len(started),
        pending=len(pending),
        expired=len(expired),
        recent=len(r.recent),
        live=live,
        jobs=r.jobs,
        launch=launch,
        retire=stopped,
    )


def secrets(r: Repo, values: dict[str, str]) -> None:
    if not values:
        have = {s["name"] for s in r.api("/actions/secrets")["secrets"]}
        missing = [s for s in SECRETS if s not in have]
        if missing:
            log(f"{r.slug} set secrets by hand: {', '.join(missing)}")
        return
    try:
        from nacl.encoding import Base64Encoder
        from nacl.public import PublicKey, SealedBox
    except ImportError:
        sys.exit("secrets need pynacl: pip install pynacl")
    key = r.api("/actions/secrets/public-key")
    box = SealedBox(PublicKey(key["key"].encode(), Base64Encoder))
    for name, value in values.items():
        sealed = base64.b64encode(box.encrypt(value.encode())).decode()
        r.api(
            f"/actions/secrets/{name}",
            "PUT",
            {"encrypted_value": sealed, "key_id": key["key_id"]},
        )
    log(f"{r.slug} secrets set: {', '.join(values)}")


def build(r: Repo, source: Path, pool: dict[str, str]) -> None:
    repo = r.get("")
    if repo is None:
        owner, name = r.slug.split("/", 1)
        login = gh(r.token, "/user")["login"]
        path = "/user/repos" if owner == login else f"/orgs/{owner}/repos"
        repo = gh(
            r.token, path, "POST", {"name": name, "private": False, "auto_init": True}
        )
        log(f"{r.slug} created")
    elif repo["private"]:
        repo = r.api("", "PATCH", {"private": False})
        log(f"{r.slug} made public")
    r.ref = r.ref or repo["default_branch"]

    path = f"/contents/.github/workflows/{r.workflow}"
    content = base64.b64encode(source.read_bytes()).decode()
    old = r.get(f"{path}?ref={r.ref}")
    if old is None or old["content"].replace("\n", "") != content:
        body = {"message": f"pool: {r.workflow}", "content": content, "branch": r.ref}
        if old:
            body["sha"] = old["sha"]
        r.api(path, "PUT", body)
        log(f"{r.slug} workflow {'updated' if old else 'added'}")

    secrets(r, pool)


def each(repos: list[Repo], fn: Callable[[Repo], None]) -> None:
    for r in repos:
        try:
            fn(r)
        except Exception as e:
            log(f"{r.slug} {type(e).__name__}: {e}")


def version() -> str:
    try:
        return metadata.version("pool")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def main() -> None:
    p = argparse.ArgumentParser(prog="pool-keeper")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--once", action="store_true")
    b = sub.add_parser("build", parents=[common])
    b.add_argument("--source", type=Path, default=WORKFLOWS)
    args = p.parse_args()

    setup(observability("pool-keeper", version()))
    try:
        repos, poll, pool, client = load(args.config)
        try:
            if args.cmd == "build":
                each(repos, lambda r: build(r, args.source / r.workflow, pool))
                return
            log(f"{len(repos)} repos, {sum(r.jobs for r in repos)} runners")
            while True:
                workers = pool_workers(pool, client)
                each(repos, lambda r: reconcile(r, workers))  # noqa: B023
                if args.once:
                    return
                time.sleep(poll)
        except KeyboardInterrupt:
            sys.exit(130)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
