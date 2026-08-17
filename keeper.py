#!/usr/bin/env python3
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
WORKFLOWS = Path(__file__).parent / ".github" / "workflows"
GRACE = 120


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
        raise ApiError(e.code, f"{method} {path} -> {e.code} {e.read()[:200].decode()}") from None
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


def reconcile(r):
    r.ref = r.ref or r.api("")["default_branch"]
    runs = r.api(f"/actions/workflows/{r.workflow}/runs?per_page=100")["workflow_runs"]
    runs = sorted((x for x in runs if x["status"] != "completed"), key=lambda x: x["created_at"], reverse=True)
    r.stopping &= {x["id"] for x in runs}

    fresh, expired = [], []
    for run in runs:
        (fresh if age(run) <= r.ttl else expired).append(run)
    for run in expired + fresh[r.jobs :]:
        if run["id"] not in r.stopping:
            r.stopping.add(run["id"])
            r.api(f"/actions/runs/{run['id']}/cancel", "POST")
            log(f"{r.slug} stopping run {run['id']}")

    now = time.time()
    r.recent = [t for t in r.recent if now - t < GRACE]
    live = min(len(fresh), r.jobs) + len(r.recent)
    for _ in range(r.jobs - live):
        r.api(f"/actions/workflows/{r.workflow}/dispatches", "POST", {"ref": r.ref})
        r.recent.append(now)
    if live < r.jobs:
        log(f"{r.slug} {live}/{r.jobs} alive, launched {r.jobs - live}")


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
        r.api(f"/actions/secrets/{name}", "PUT", {"encrypted_value": sealed, "key_id": key["key_id"]})
    log(f"{r.slug} secrets set: {', '.join(values)}")


def build(r, source, pool):
    repo = r.get("")
    if repo is None:
        owner, name = r.slug.split("/", 1)
        login = gh(r.token, "/user")["login"]
        path = "/user/repos" if owner == login else f"/orgs/{owner}/repos"
        repo = gh(r.token, path, "POST", {"name": name, "private": False, "auto_init": True})
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
    p = argparse.ArgumentParser(prog="keeper")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--once", action="store_true")
    b = sub.add_parser("build", parents=[common])
    b.add_argument("--source", type=Path, default=WORKFLOWS)
    args = p.parse_args()

    repos, poll, pool = load(args.config)
    if args.cmd == "build":
        each(repos, lambda r: build(r, args.source / r.workflow, pool))
        return

    log(f"{len(repos)} repos, {sum(r.jobs for r in repos)} runners")
    while True:
        each(repos, reconcile)
        if args.once:
            return
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
