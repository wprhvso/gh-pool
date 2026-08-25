from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from gh_pool.fleet.runners.config import (
    FLEET_INTERVAL,
    STATS_INTERVAL,
    WORKER_STALE,
)
from gh_pool.fleet.runners.errors import HttpError, RateLimited, RunnerError
from gh_pool.fleet.runners.metrics import FLEET_SIZE
from gh_pool.fleet.runners.policy import scale
from gh_pool.fleet.runners.provision import NOT_FOUND, cancel
from gh_pool.fleet.runners.state import TIMEOUT_GRACE
from gh_pool.status import LIVE, TaskStatus

if TYPE_CHECKING:
    from gh_pool.fleet.runners.state import Ctx

log = logging.getLogger(__name__)


def adopt(ctx: Ctx) -> int:
    try:
        rows = ctx.pool.mine(ctx.slug, ctx.target.label)
    except RunnerError as exc:
        log.warning("%s: не спросил у пула свои задачи: %s", ctx.slug, exc)
        return 0
    for task_id, name, status in rows:
        ctx.fleet.born(task_id, name)
        ctx.fleet.mark(task_id, status)
    if rows:
        log.info("%s: подобрал своих раннеров из пула: %s", ctx.slug, len(rows))
    return len(rows)


def held(ctx: Ctx) -> set[str]:
    try:
        rows = ctx.pool.workers()
    except RunnerError as exc:
        log.debug("%s: не спросил воркеров: %s", ctx.slug, exc)
        return set()
    return {str(row.get("task_id")) for row in rows if row.get("task_id")}


def why(ctx: Ctx, task_id: str, state: dict[str, Any]) -> str:
    error = str(state.get("error") or "").strip()
    try:
        tail = ctx.pool.tail(task_id, 600).strip().splitlines()
    except RunnerError:
        tail = []
    return f"{error or 'без причины'}: {tail[-1] if tail else ''}"[:300]


def reconcile(ctx: Ctx) -> None:
    taken = held(ctx) if ctx.fleet.tracking() else set()
    slots = ctx.fleet.slots()
    blind = 0
    for slot in slots:
        if slot.lived() > ctx.target.lifetime + TIMEOUT_GRACE:
            log.warning(
                "%s: раннер %s живёт дольше отпущенного, снимаю", ctx.slug, slot.name
            )
            cancel(ctx, slot.task_id)
            ctx.fleet.drop(slot.task_id)
            continue
        try:
            state = ctx.pool.state(slot.task_id)
        except HttpError as exc:
            if exc.status != NOT_FOUND:
                log.debug("%s: не спросил задачу %s: %s", ctx.slug, slot.task_id, exc)
                blind += 1
                continue
            log.warning("%s: пул забыл задачу раннера %s", ctx.slug, slot.name)
            ctx.fleet.drop(slot.task_id)
            continue
        except RunnerError as exc:
            log.debug("%s: не спросил задачу %s: %s", ctx.slug, slot.task_id, exc)
            blind += 1
            continue
        status = str(state.get("status") or "")
        if status not in LIVE:
            ctx.fleet.drop(slot.task_id)
            log.info("%s: раннер %s отработал: %s", ctx.slug, slot.name, status)
            if status in (TaskStatus.FAILED, TaskStatus.LOST):
                log.warning(
                    "%s: %s — %s", ctx.slug, slot.name, why(ctx, slot.task_id, state)
                )
            continue

        ctx.fleet.mark(slot.task_id, status)
        if (
            status == TaskStatus.RUNNING
            and taken
            and slot.task_id not in taken
            and slot.age() > WORKER_STALE
        ):
            log.warning(
                "%s: воркер с раннером %s пропал, снимаю задачу", ctx.slug, slot.name
            )
            cancel(ctx, slot.task_id)

    if blind:
        log.warning(
            "%s: пул не ответил по %s слотам из %s, флот не сверен",
            ctx.slug,
            blind,
            len(slots),
        )


def reconcile_loop(ctx: Ctx, stop: threading.Event) -> None:
    asked = 0.0
    while not stop.wait(FLEET_INTERVAL):
        try:
            reconcile(ctx)
            FLEET_SIZE.set(ctx.fleet.size(), {"repo": ctx.slug})
            if time.monotonic() - asked > STATS_INTERVAL:
                ctx.latest.set(ctx.api.statistics(ctx.scale_set_id))
                asked = time.monotonic()
            scale(ctx, ctx.latest.get(), "сверка", shrink=True)
        except RateLimited as exc:
            log.debug("сверка ждёт сброса лимита: %s", exc)
        except RunnerError as exc:
            log.debug("%s: не сверил флот: %s", ctx.slug, exc)
        except Exception:
            log.exception("сверка флота сорвалась")
