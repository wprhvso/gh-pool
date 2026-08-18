import argparse
import base64
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

API = "https://api.github.com"
KEYS = ("token", "workflow", "jobs", "ttl", "ref")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SECRETS = ("POOL_SERVER", "POOL_TOKEN")
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# Окно между dispatch и появлением прогона в списке — только на видимость API.
GRACE = 30
# Прогон не обслуживает сразу: бутстрап тулчейна ~140 с. Пока он моложе этого,
# считаем его прогревающимся; старше и всё ещё не обслуживает — это зомби, и
# место под него надо освободить, а не ждать шесть часов.
BOOT_GRACE = 240
# Сколько прогонов keeper вправе погасить за один тик. Массовая отмена — это
# массовая потеря задач: пул считает их lost только через LOST_AFTER и не
# перезапускает, а CI-джобы уходят на второй круг.
RETIRE_BUDGET = 1
# Сколько поднимать за тик, чтобы холодный старт не шёл стадом.
MAX_LAUNCH = 5
# WORKERS на сервере в памяти: сразу после его рестарта он пуст. Столько ждём,
# прежде чем верить нулям (2 × WORKER_STALE сервера).
POOL_WARMUP = 240


class ApiError(RuntimeError):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def gh(token, path, method="GET", body=None):
    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise ApiError(
            e.code, f"{method} {path} -> {e.code} {e.read()[:200].decode()}"
        ) from None
    return json.loads(data) if data else {}


def secs(v):
    return float(v) if isinstance(v, (int, float)) else float(v[:-1]) * UNITS[v[-1]]


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def age(run):
    return time.time() - datetime.fromisoformat(run["created_at"]).timestamp()


@dataclass
class Repo:
    slug: str
    token: str
    workflow: str = "pool.yml"
    jobs: int = 20
    ttl: str | float = "6h"
    ref: str = ""
    recent: list = field(default_factory=list)
    stopping: set = field(default_factory=set)

    def __post_init__(self):
        self.ttl = secs(self.ttl)

    def api(self, path, method="GET", body=None):
        return gh(self.token, f"/repos/{self.slug}{path}", method, body)

    def get(self, path):
        try:
            return self.api(path)
        except ApiError as e:
            if e.code == 404:
                return None
            raise


def load(path):
    raw = tomllib.loads(path.read_text())
    repos = raw.pop("repos", {})
    pool = {f"POOL_{k.upper()}": str(v) for k, v in raw.pop("pool", {}).items()}
    out = []
    for slug, value in repos.items():
        cfg = {**raw, **({"token": value} if isinstance(value, str) else value)}
        if "token" not in cfg:
            sys.exit(f"{slug}: no token")
        out.append(Repo(slug, **{k: cfg[k] for k in KEYS if k in cfg}))
    if not out:
        sys.exit(f"{path}: no repos")
    return out, secs(raw.get("poll", 60)), pool


def pool_serving(pool):
    """Сколько воркеров реально лизят задачи. None — судить нельзя, считаем по прогонам."""
    base = (pool.get("POOL_SERVER") or "").rstrip("/")
    if not base:
        return None
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=10) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError) as e:
        log(f"pool healthz недоступен: {e}")
        return None
    up = time.time() - float(data.get("started_at") or 0)
    if up < POOL_WARMUP:
        log(f"pool поднялся {up:.0f}с назад, счётчик воркеров ещё не наполнился")
        return None
    return int(data.get("workers") or 0)


def reconcile(r, serving=None):
    r.ref = r.ref or r.api("")["default_branch"]
    runs = r.api(f"/actions/workflows/{r.workflow}/runs?per_page=100")["workflow_runs"]
    runs = sorted(
        (x for x in runs if x["status"] != "completed"),
        key=lambda x: x["created_at"],
        reverse=True,
    )
    r.stopping &= {x["id"] for x in runs}

    fresh, expired = [], []
    for run in runs:
        (fresh if age(run) <= r.ttl else expired).append(run)

    # runs идёт новыми вперёд. Лишние надо срезать с этого конца: свежий прогон
    # ещё разворачивает тулчейн и никого не обслуживает, а старый почти наверняка
    # держит CI-джобу. Срезав старые, мы роняем задачу в lost (терминально) и
    # отправляем джобу на второй круг вместе с полным бутстрапом.
    surplus = fresh[: max(0, len(fresh) - r.jobs)]
    victims = [(run, "expired") for run in expired] + [
        (run, "surplus") for run in surplus
    ]
    stopped = 0
    for run, why in victims:
        if run["id"] in r.stopping:
            continue
        if stopped >= RETIRE_BUDGET:
            log(f"{r.slug} {len(victims) - stopped} more to retire, next tick")
            break
        r.stopping.add(run["id"])
        r.api(f"/actions/runs/{run['id']}/cancel", "POST")
        stopped += 1
        log(f"{r.slug} stopping run {run['id']} reason={why} age={age(run):.0f}s")

    now = time.time()
    r.recent = [t for t in r.recent if now - t < GRACE]
    young = [x for x in fresh if age(x) < BOOT_GRACE]
    if serving is None:
        alive = min(len(fresh), r.jobs)
    else:
        # Обслуживающие почти наверняка из прогретых, молодые ещё бутстрапятся.
        # Прогретый, но не обслуживающий — зомби: раньше он числился живым до
        # самого ttl, и ёмкость тихо проседала.
        alive = min(serving, len(fresh) - len(young)) + len(young)
    live = min(alive, r.jobs) + len(r.recent)
    launch = min(max(0, r.jobs - live), MAX_LAUNCH)
    for _ in range(launch):
        r.api(f"/actions/workflows/{r.workflow}/dispatches", "POST", {"ref": r.ref})
        r.recent.append(now)
    log(
        f"{r.slug} serving={serving} young={len(young)} fresh={len(fresh)} "
        f"recent={len(r.recent)} live={live}/{r.jobs} launch={launch} retire={stopped}"
    )


def secrets(r, values):
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


def build(r, source, pool):
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


def each(repos, fn):
    for r in repos:
        try:
            fn(r)
        except Exception as e:
            log(f"{r.slug} {type(e).__name__}: {e}")


def main():
    p = argparse.ArgumentParser(prog="pool-keeper")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--once", action="store_true")
    b = sub.add_parser("build", parents=[common])
    b.add_argument("--source", type=Path, default=WORKFLOWS)
    args = p.parse_args()

    repos, poll, pool = load(args.config)
    try:
        if args.cmd == "build":
            each(repos, lambda r: build(r, args.source / r.workflow, pool))
            return
        log(f"{len(repos)} repos, {sum(r.jobs for r in repos)} runners")
        while True:
            # /healthz отдаёт общее число воркеров без разбивки по репозиториям,
            # так что доверять ему можно только когда репозиторий один.
            serving = pool_serving(pool) if len(repos) == 1 else None
            each(repos, lambda r: reconcile(r, serving))  # noqa: B023
            if args.once:
                return
            time.sleep(poll)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
