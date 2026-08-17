#!/usr/bin/env python3
import argparse
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
GRACE = 120


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
        req = urllib.request.Request(
            f"{API}/repos/{self.slug}{path}",
            data=json.dumps(body).encode() if body else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "pool-keeper",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path or '/'} -> {e.code} {e.read()[:200].decode()}") from None
        return json.loads(data) if data else {}


def load(path):
    raw = tomllib.loads(path.read_text())
    repos = raw.pop("repos", {})
    out = []
    for slug, value in repos.items():
        cfg = {**raw, **({"token": value} if isinstance(value, str) else value)}
        if "token" not in cfg:
            sys.exit(f"{slug}: no token")
        out.append(Repo(slug, **{k: cfg[k] for k in KEYS if k in cfg}))
    if not out:
        sys.exit(f"{path}: no repos")
    return out, secs(raw.get("poll", 60))


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


def main():
    p = argparse.ArgumentParser(prog="keeper")
    p.add_argument("config", type=Path)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    repos, poll = load(args.config)
    log(f"{len(repos)} repos, {sum(r.jobs for r in repos)} runners")
    while True:
        for r in repos:
            try:
                reconcile(r)
            except Exception as e:
                log(f"{r.slug} {type(e).__name__}: {e}")
        if args.once:
            return
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
