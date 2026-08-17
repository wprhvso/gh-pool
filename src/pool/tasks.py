import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REGISTRY = {}


def task(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn

    return deco


@task("echo")
def echo(payload, result_path):
    for i in range(payload.get("count", 3)):
        print(f"tick {i}")
        time.sleep(payload.get("delay", 1))
    return {"echoed": payload}


@task("shell")
def shell(payload, result_path):
    cmd = payload["cmd"]
    print(f"$ {cmd}")
    p = subprocess.run(cmd, shell=True, stdout=sys.stdout, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise RuntimeError(f"exit code {p.returncode}")
    return {"returncode": p.returncode}


@task("fetch")
def fetch(payload, result_path):
    url = payload["url"]
    print(f"fetching {url}")
    total = 0
    with urllib.request.urlopen(url, timeout=60) as r, open(result_path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            if total % (1 << 22) < (1 << 16):
                print(f"{total} bytes")
    print(f"done, {total} bytes")


@task("python")
def python_eval(payload, result_path):
    code = payload["code"]
    scope = {"payload": payload, "result_path": Path(result_path)}
    exec(code, scope)
    out = scope.get("result")
    if out is not None:
        return out
