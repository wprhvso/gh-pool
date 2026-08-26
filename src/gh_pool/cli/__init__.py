import argparse
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any, NoReturn

import httpx
from yaol import SpanKind, from_env, inject_headers, setup, shutdown, span

from gh_pool.cli import chrome, shell
from gh_pool.core.obs import version
from gh_pool.status import FINISHED

SERVER = os.getenv("GH_POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("GH_POOL_CLIENT_TOKEN", "dev-client")
CHROME_TOKEN = os.getenv("GH_POOL_TOKEN", "")
POLL = 0.5
CODES: dict[str | None, int] = {"done": 0, "failed": 1, "cancelled": 2, "lost": 3}
SESSION_OVER = frozenset({"closed", "dead"})


@cache
def http() -> httpx.Client:
    return httpx.Client(
        base_url=SERVER,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=httpx.Timeout(30.0, read=300.0),
    )


def die(msg: str, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(code)


def call(method: str, path: str, **kw: Any) -> httpx.Response:
    try:
        r = http().request(method, path, **kw)
    except Exception as e:
        die(f"server unreachable: {type(e).__name__}: {e}")
    if r.status_code >= 400:
        die(f"{r.status_code}: {r.text[:300]}")
    return r


def chrome_call(method: str, path: str, **kw: Any) -> httpx.Response:
    if not CHROME_TOKEN:
        die("GH_POOL_TOKEN is not set: browser sessions have a token of their own")
    headers = dict(kw.pop("headers", None) or {})
    headers["Authorization"] = f"Bearer {CHROME_TOKEN}"
    return call(method, path, headers=headers, **kw)


def parse_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload:
        return json.loads(args.payload)
    if args.payload_file:
        return json.loads(Path(args.payload_file).read_text())
    out = {}
    for item in args.set or []:
        if "=" not in item:
            die(f"bad -p value: {item}")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit}"
    return f"{int(d)}s"


def follow(tid: str, offset: int = 0) -> str | None:
    status = None
    while True:
        r = call("GET", f"/v1/tasks/{tid}/events", params={"offset": offset})
        if r.content:
            sys.stdout.buffer.write(r.content)
            sys.stdout.buffer.flush()
        offset = int(r.headers.get("X-Event-Offset", offset))
        new_status = r.headers.get("X-Task-Status")
        if (
            new_status in FINISHED
            and status == new_status
            and offset >= int(r.headers.get("X-Event-Size", 0))
        ):
            break
        status = new_status
        if status in FINISHED:
            continue
        time.sleep(POLL)
    return status


def finish(tid: str, status: str | None) -> NoReturn:
    t = call("GET", f"/v1/tasks/{tid}").json()
    err = t.get("error")
    print(f"\n--- {status}" + (f": {err}" if err else ""), file=sys.stderr)
    sys.exit(CODES.get(status, 1))


def cmd_submit(args: argparse.Namespace) -> None:
    payload = parse_payload(args)
    with span(
        "pool.submit", {"pool.task.type": args.type}, kind=SpanKind.PRODUCER
    ) as active:
        payload["trace"] = dict(inject_headers())
        body = {"type": args.type, "payload": payload}
        tid = call("POST", "/v1/tasks", json=body).json()["task_id"]
        active.set_attribute("pool.task.id", str(tid))
    if not args.follow:
        print(tid)
        return
    print(f"--- {tid}", file=sys.stderr)
    finish(tid, follow(tid))


def leased(tid: str) -> dict[str, Any]:
    waiting = False
    while True:
        task = call("GET", f"/v1/tasks/{tid}").json()
        if task["status"] != "pending":
            return task
        if not waiting:
            waiting = True
            print("--- waiting for a free runner", file=sys.stderr)
        time.sleep(POLL)


def cmd_sh(args: argparse.Namespace) -> None:
    tid = args.id
    if tid is None:
        body = {"type": "shell", "payload": shell.payload(args.command)}
        tid = call("POST", "/v1/tasks", json=body).json()["task_id"]
    task = leased(tid)
    if task["status"] != "running":
        die(f"task {task['status']}, no shell for it", CODES.get(task["status"], 1))
    print(
        f"--- {tid} on {task.get('worker_id') or '?'}, detach with ~.", file=sys.stderr
    )
    status = shell.run(tid, SERVER, TOKEN)
    if status == "detached":
        print(f"--- detached, come back with: gh-pool sh {tid}", file=sys.stderr)
        return
    if status == "gone":
        status = call("GET", f"/v1/tasks/{tid}").json()["status"]
    finish(tid, status)


def start_session(args: argparse.Namespace) -> str:
    params = {
        name: value
        for name, value in (("width", args.width), ("height", args.height))
        if value is not None
    }
    body: dict[str, Any] = {"profile": args.profile}
    if params:
        body["params"] = params
    return str(chrome_call("POST", "/sessions", json=body).json()["id"])


def settled(sid: str, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    waiting = False
    while True:
        status = str(chrome_call("GET", f"/sessions/{sid}").json()["status"])
        if status != "pending" or time.monotonic() >= deadline:
            return status
        if not waiting:
            waiting = True
            print("--- waiting for a runner to bring the desktop up", file=sys.stderr)
        time.sleep(POLL)


def cmd_chrome(args: argparse.Namespace) -> None:
    if args.session and (args.profile or args.width or args.height):
        die("--session opens a desktop that already runs, it does not make one")
    sid = args.session or start_session(args)
    url = chrome.player(SERVER, sid) if args.player else chrome.desktop(SERVER, sid)
    print(url)
    print(f"--- {sid}, log in as {chrome.USER} with $GH_POOL_TOKEN", file=sys.stderr)
    if not args.player:
        print(f"--- player at {chrome.player(SERVER, sid)}", file=sys.stderr)
    if args.no_open:
        return
    if args.wait > 0:
        status = settled(sid, args.wait)
        if status in SESSION_OVER:
            die(f"session {status}, no desktop to open")
        if status == "pending":
            print("--- no desktop yet, opening anyway", file=sys.stderr)
    if not chrome.open_new(url):
        print("--- no browser here to open it with", file=sys.stderr)


def cmd_events(args: argparse.Namespace) -> None:
    if args.follow:
        finish(args.id, follow(args.id, args.offset))
    r = call("GET", f"/v1/tasks/{args.id}/events", params={"offset": args.offset})
    sys.stdout.buffer.write(r.content)


def cmd_status(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            call("GET", f"/v1/tasks/{args.id}").json(), indent=2, ensure_ascii=False
        )
    )


def cmd_list(args: argparse.Namespace) -> None:
    params = {"limit": args.limit}
    if args.status:
        params["status"] = args.status
    rows = call("GET", "/v1/tasks", params=params).json()
    if not rows:
        print("nothing")
        return
    print(f"{'ID':34} {'TYPE':14} {'STATUS':10} {'AGE':>6} {'EVENTS':>10}  WORKER")
    for t in rows:
        print(
            f"{t['id']:34} {t['type'][:14]:14} {t['status']:10} "
            f"{ago(t['created_at']):>6} {t['event_size']:>10}  {t.get('worker_id') or '-'}"
        )


def cmd_cancel(args: argparse.Namespace) -> None:
    print(json.dumps(call("POST", f"/v1/tasks/{args.id}/cancel").json()))


def cmd_retry(args: argparse.Namespace) -> None:
    tid = call("POST", f"/v1/tasks/{args.id}/retry").json()["task_id"]
    if not args.follow:
        print(tid)
        return
    print(f"--- {tid}", file=sys.stderr)
    finish(tid, follow(tid))


def cmd_put(args: argparse.Namespace) -> None:
    with Path(args.file).open("rb") as f:
        meta = call(
            "PUT",
            f"/v1/artifacts/{args.key}",
            content=f,
            params={"task_id": args.task} if args.task else None,
        )
    print(json.dumps(meta.json()))


def cmd_get(args: argparse.Namespace) -> None:
    with http().stream("GET", f"/v1/artifacts/{args.key}") as r:
        if r.status_code >= 400:
            r.read()
            die(f"{r.status_code}: {r.text[:300]}")
        out = Path(args.output).open("wb") if args.output else sys.stdout.buffer  # noqa: SIM115
        try:
            for chunk in r.iter_bytes():
                out.write(chunk)
        finally:
            if args.output:
                out.close()
                print(f"saved to {args.output}", file=sys.stderr)


def cmd_rm(args: argparse.Namespace) -> None:
    print(json.dumps(call("DELETE", f"/v1/artifacts/{args.key}").json()))


def cmd_artifacts(args: argparse.Namespace) -> None:
    rows = call(
        "GET", "/v1/artifacts", params={"prefix": args.prefix, "limit": args.limit}
    ).json()
    if not rows:
        print("nothing")
        return
    print(f"{'KEY':40} {'SIZE':>12} {'AGE':>6}  TASK")
    for a in rows:
        print(
            f"{a['key'][:40]:40} {a['size']:>12} {ago(a['created_at']):>6}  {a.get('task_id') or '-'}"
        )


def cmd_workers(args: argparse.Namespace) -> None:
    rows = call("GET", "/v1/workers").json()
    if not rows:
        print("no workers")
        return
    print(f"{'WORKER':30} {'IDLE':>6}  TASK")
    for w in rows:
        print(f"{w['id']:30} {w['idle_for']:>5.0f}s  {w.get('task_id') or '-'}")


def cmd_health(args: argparse.Namespace) -> None:
    print(json.dumps(call("GET", "/healthz").json(), indent=2))


def observe() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        setup(
            replace(
                from_env("pool-cli", service_version=version()),
                log_level="WARNING",
                export_logs=False,
                export_metrics=False,
            )
        )
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            _ = handler.setStream(sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(prog="gh-pool")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("type")
    s.add_argument("-p", "--set", action="append")
    s.add_argument("--payload")
    s.add_argument("--payload-file")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("sh")
    s.add_argument("id", nargs="?")
    s.add_argument("-c", "--command")
    s.set_defaults(fn=cmd_sh)

    s = sub.add_parser("chrome")
    s.add_argument("profile", nargs="?")
    s.add_argument("-s", "--session")
    s.add_argument("-n", "--no-open", action="store_true")
    s.add_argument("-p", "--player", action="store_true")
    s.add_argument("-w", "--wait", type=float, default=600.0)
    s.add_argument("--width", type=int)
    s.add_argument("--height", type=int)
    s.set_defaults(fn=cmd_chrome)

    s = sub.add_parser("events")
    s.add_argument("id")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-o", "--offset", type=int, default=0)
    s.set_defaults(fn=cmd_events)

    s = sub.add_parser("status")
    s.add_argument("id")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("list")
    s.add_argument("-s", "--status")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("cancel")
    s.add_argument("id")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("retry")
    s.add_argument("id")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=cmd_retry)

    s = sub.add_parser("put")
    s.add_argument("key")
    s.add_argument("file")
    s.add_argument("-t", "--task")
    s.set_defaults(fn=cmd_put)

    s = sub.add_parser("get")
    s.add_argument("key")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("rm")
    s.add_argument("key")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("artifacts")
    s.add_argument("prefix", nargs="?", default="")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(fn=cmd_artifacts)

    sub.add_parser("workers").set_defaults(fn=cmd_workers)
    sub.add_parser("health").set_defaults(fn=cmd_health)

    args = p.parse_args()
    observe()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        shutdown(timeout_millis=2000)


if __name__ == "__main__":
    main()
