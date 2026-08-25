from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from yaol import attached, capture, record_exception, span

from gh_pool.fleet.runners.config import SUBMIT_WORKERS
from gh_pool.fleet.runners.errors import HttpError, RunnerError
from gh_pool.fleet.runners.metrics import RUNNERS_LAUNCHED
from gh_pool.fleet.runners.state import CODE, TIMEOUT_GRACE

if TYPE_CHECKING:
    from gh_pool.fleet.runners.state import Ctx

log = logging.getLogger(__name__)

NOT_FOUND = 404


def launch(ctx: Ctx, version: str) -> bool:
    with span("runner.launch", {"repo": ctx.slug, "runner.version": version}) as active:
        try:
            runner_id, name, jit = ctx.api.jit(ctx.scale_set_id)
        except RunnerError as exc:
            log.warning("%s: не выписал JIT: %s", ctx.slug, exc)
            record_exception(exc)
            RUNNERS_LAUNCHED.add(1, {"repo": ctx.slug, "outcome": "failure"})
            return False

        active.set_attribute("runner.name", name)
        try:
            task_id = ctx.pool.submit(
                CODE,
                "agent",
                {
                    "jit": jit,
                    "version": version,
                    "name": name,
                    "work": ctx.target.work,
                    "idle": ctx.target.idle,
                    "lifetime": ctx.target.lifetime,
                    "sha256": ctx.target.sha256,
                    "slug": ctx.slug,
                    "label": ctx.target.label,
                },
                timeout=ctx.target.lifetime + TIMEOUT_GRACE,
            )
        except RunnerError as exc:
            log.warning("%s: пул не принял раннера: %s", ctx.slug, exc)
            record_exception(exc)
            RUNNERS_LAUNCHED.add(1, {"repo": ctx.slug, "outcome": "failure"})
            ctx.api.forget(runner_id)
            return False

        active.set_attribute("pool.task_id", task_id)
        ctx.fleet.born(task_id, name, runner_id)
        RUNNERS_LAUNCHED.add(1, {"repo": ctx.slug, "outcome": "success"})
        log.info("%s: раннер %s уехал в пул задачей %s", ctx.slug, name, task_id)
        return True


def launch_many(ctx: Ctx, count: int, version: str) -> int:
    if count == 1:
        return int(launch(ctx, version))
    stumbled = threading.Event()
    parent = capture()

    def once() -> bool:
        if stumbled.is_set():
            return False
        with attached(parent):
            if launch(ctx, version):
                return True
        stumbled.set()
        return False

    with ThreadPoolExecutor(
        max_workers=min(count, SUBMIT_WORKERS),
        thread_name_prefix="submit",
    ) as pool:
        sending = [pool.submit(once) for _ in range(count)]
        return sum(int(item.result()) for item in sending)


def cancel(ctx: Ctx, task_id: str) -> bool:
    try:
        ctx.pool.cancel(task_id)
    except HttpError as exc:
        if exc.status != NOT_FOUND:
            log.debug("%s: задача %s не отменилась: %s", ctx.slug, task_id, exc)
            return False
    except RunnerError as exc:
        log.debug("%s: задача %s не отменилась: %s", ctx.slug, task_id, exc)
        return False
    gone = ctx.fleet.drop(task_id)
    if gone is not None and gone.runner_id:
        ctx.api.forget(gone.runner_id)
    return True


def shrink(ctx: Ctx, surplus: int) -> int:
    dropped = sum(cancel(ctx, slot.task_id) for slot in ctx.fleet.spare()[:surplus])
    if dropped:
        log.info("%s: неуехавших раннеров снял: %s", ctx.slug, dropped)
    return dropped
