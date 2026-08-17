import argparse
import json
import os
import sys
import time
from datetime import datetime

import httpx

SERVER = os.getenv("POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("POOL_CLIENT_TOKEN", "dev-client")
POLL = 0.5
TERMINAL = ("done", "failed", "cancelled", "lost")
CODES = {"done": 0, "failed": 1, "cancelled": 2, "lost": 3}

http = httpx.Client(
    base_url=SERVER,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=httpx.Timeout(30.0, read=300.0),
)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def call(method, path, **kw):
    try:
        r = http.request(method, path, **kw)
    except Exception as e:
        die(f"server unreachable: {type(e).__name__}: {e}")
    if r.status_code >= 400:
        die(f"{r.status_code}: {r.text[:300]}")
    return r


def parse_payload(args):
    if args.payload:
        return json.loads(args.payload)
    if args.payload_file:
        return json.loads(open(args.payload_file).read())
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


def ago(ts):
    if not ts:
        return "-"
    d = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit}"
    return f"{int(d)}s"


def follow(tid, offset=0):
    status = None
    while True:
        r = call("GET", f"/v1/tasks/{tid}/log", params={"offset": offset})
        if r.content:
            sys.stdout.buffer.write(r.content)
            sys.stdout.buffer.flush()
        offset = int(r.headers.get("X-Log-Offset", offset))
        new_status = r.headers.get("X-Task-Status")
        if new_status in TERMINAL and status == new_status and offset >= int(r.headers.get("X-Log-Size", 0)):
            break
        status = new_status
        if status in TERMINAL:
            continue
        time.sleep(POLL)
    return status


def finish(tid, status):
    t = call("GET", f"/v1/tasks/{tid}").json()
    err = t.get("error")
    print(f"\n--- {status}" + (f": {err}" if err else ""), file=sys.stderr)
    if t.get("result_name"):
        print(f"--- result: {SERVER}/v1/tasks/{tid}/result", file=sys.stderr)
    sys.exit(CODES.get(status, 1))


def cmd_submit(args):
    body = {"type": args.type, "payload": parse_payload(args)}
    tid = call("POST", "/v1/tasks", json=body).json()["task_id"]
    if not args.follow:
        print(tid)
        return
    print(f"--- {tid}", file=sys.stderr)
    finish(tid, follow(tid))


def cmd_logs(args):
    if args.follow:
        finish(args.id, follow(args.id, args.offset))
    r = call("GET", f"/v1/tasks/{args.id}/log", params={"offset": args.offset})
    sys.stdout.buffer.write(r.content)


def cmd_status(args):
    print(json.dumps(call("GET", f"/v1/tasks/{args.id}").json(), indent=2, ensure_ascii=False))


def cmd_list(args):
    params = {"limit": args.limit}
    if args.status:
        params["status"] = args.status
    rows = call("GET", "/v1/tasks", params=params).json()
    if not rows:
        print("nothing")
        return
    print(f"{'ID':34} {'TYPE':14} {'STATUS':10} {'AGE':>6} {'LOG':>10}  WORKER")
    for t in rows:
        print(
            f"{t['id']:34} {t['type'][:14]:14} {t['status']:10} "
            f"{ago(t['created_at']):>6} {t['log_size']:>10}  {t.get('worker_id') or '-'}"
        )


def cmd_result(args):
    with http.stream("GET", f"/v1/tasks/{args.id}/result") as r:
        if r.status_code >= 400:
            r.read()
            die(f"{r.status_code}: {r.text[:300]}")
        out = open(args.output, "wb") if args.output else sys.stdout.buffer
        try:
            for chunk in r.iter_bytes():
                out.write(chunk)
        finally:
            if args.output:
                out.close()
                print(f"saved to {args.output}", file=sys.stderr)


def cmd_cancel(args):
    print(json.dumps(call("POST", f"/v1/tasks/{args.id}/cancel").json()))


def cmd_retry(args):
    tid = call("POST", f"/v1/tasks/{args.id}/retry").json()["task_id"]
    if not args.follow:
        print(tid)
        return
    print(f"--- {tid}", file=sys.stderr)
    finish(tid, follow(tid))


def cmd_workers(args):
    rows = call("GET", "/v1/workers").json()
    if not rows:
        print("no workers")
        return
    print(f"{'WORKER':30} {'IDLE':>6}  TASK")
    for w in rows:
        print(f"{w['id']:30} {w['idle_for']:>5.0f}s  {w.get('task_id') or '-'}")


def cmd_health(args):
    print(json.dumps(call("GET", "/healthz").json(), indent=2))


def main():
    p = argparse.ArgumentParser(prog="pool")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("type")
    s.add_argument("-p", "--set", action="append")
    s.add_argument("--payload")
    s.add_argument("--payload-file")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("logs")
    s.add_argument("id")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-o", "--offset", type=int, default=0)
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("status")
    s.add_argument("id")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("list")
    s.add_argument("-s", "--status")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("result")
    s.add_argument("id")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_result)

    s = sub.add_parser("cancel")
    s.add_argument("id")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("retry")
    s.add_argument("id")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=cmd_retry)

    sub.add_parser("workers").set_defaults(fn=cmd_workers)
    sub.add_parser("health").set_defaults(fn=cmd_health)

    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
