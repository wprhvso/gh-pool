import importlib
import json
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any, NoReturn

from gh_pool.worker.errors import Cancelled

UNKNOWN_TYPE = 2
CANCELLED = 75


def run_exec(ttype: str, payload_file: str) -> NoReturn:
    tasks = importlib.import_module(os.getenv("GH_POOL_TASKS", "gh_pool.tasks"))
    fn = tasks.REGISTRY.get(ttype)
    if fn is None:
        print(f"unknown task type: {ttype}")  # noqa: T201
        sys.exit(UNKNOWN_TYPE)

    def on_term(*_: Any) -> NoReturn:
        raise Cancelled

    signal.signal(signal.SIGTERM, on_term)
    payload = json.loads(Path(payload_file).read_text())
    try:
        fn(payload)
    except Cancelled:
        print("[task] cancelled")  # noqa: T201
        sys.exit(CANCELLED)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
