from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from yaol import record_exception, span

from gh_pool.fleet.runners.config import QUEUE_MESSAGE_TYPE
from gh_pool.fleet.runners.errors import HttpError, RunnerError
from gh_pool.fleet.runners.http import backoff
from gh_pool.fleet.runners.metrics import JOBS_ACQUIRED
from gh_pool.fleet.runners.models import Stats, to_int
from gh_pool.fleet.runners.policy import scale
from gh_pool.fleet.runners.wire import read_jobs

if TYPE_CHECKING:
    from gh_pool.fleet.runners.models import Session
    from gh_pool.fleet.runners.state import Ctx

log = logging.getLogger(__name__)

UNAUTHORIZED = 401


def acquire(ctx: Ctx, session: Session, ids: list[int]) -> int:
    if not ids:
        return 0
    try:
        taken = ctx.api.acquire(ctx.scale_set_id, session, ids)
    except HttpError as exc:
        if exc.status == UNAUTHORIZED:
            raise
        log.warning("%s: не забрал job'ы %s: %s", ctx.slug, ids, exc)
        record_exception(exc)
        return 0
    JOBS_ACQUIRED.add(taken, {"repo": ctx.slug})
    log.info("%s: забрал job'ов: %s из %s", ctx.slug, taken, len(ids))
    return taken


def handle(ctx: Ctx, session: Session, message: dict[str, Any]) -> None:
    kind = message.get("messageType")
    if kind != QUEUE_MESSAGE_TYPE:
        log.debug("пропускаю сообщение типа %r", kind)
        return
    with span(
        "queue.message",
        {"repo": ctx.slug, "queue.message_id": to_int(message.get("messageId"))},
    ) as active:
        raw = message.get("statistics")
        stats = ctx.latest.get() if raw is None else ctx.latest.set(Stats.parse(raw))
        offered, retired = read_jobs(str(message.get("body") or ""))
        active.set_attributes(
            {"queue.offered": len(offered), "queue.retired": len(retired)}
        )
        for name in retired:
            if ctx.fleet.spend(name):
                log.info("%s: раннер %s своё отработал", ctx.slug, name)
        taken = acquire(ctx, session, offered)
        if taken:
            stats = ctx.latest.took(taken)
        active.set_attribute("queue.acquired", taken)
        scale(ctx, stats, "сообщение")


def pick_up(ctx: Ctx, session: Session, stats: Stats, note: str) -> None:
    if stats.available or stats.assigned:
        ids = [
            found
            for job in ctx.api.acquirable(ctx.scale_set_id)
            if (found := to_int(job.get("runnerRequestId")))
        ]
        taken = acquire(ctx, session, ids)
        if taken:
            stats = ctx.latest.took(taken)
    scale(ctx, stats, note)


def ack(ctx: Ctx, session: Session, message: dict[str, Any]) -> None:
    message_id = to_int(message.get("messageId"))
    if not message_id:
        return
    try:
        ctx.api.ack(session, message_id)
    except RunnerError as exc:
        log.warning("%s: не подтвердил сообщение %s: %s", ctx.slug, message_id, exc)


def tick(ctx: Ctx, session: Session, last_id: int, stop: threading.Event) -> int:
    try:
        message = ctx.api.poll(session, last_id, ctx.target.jobs)
    except HttpError:
        raise
    except RunnerError as exc:
        log.warning("опрос очереди не удался: %s", exc)
        stop.wait(backoff(1))
        message = None

    if stop.is_set():
        return last_id
    if message is None:
        pick_up(ctx, session, ctx.latest.get(), "тишина")
        return last_id

    fresh_id = to_int(message.get("messageId"))
    if fresh_id and fresh_id <= last_id:
        log.debug("сообщение %s уже обработано, подтверждаю снова", fresh_id)
        ack(ctx, session, message)
        return last_id

    handle(ctx, session, message)
    ack(ctx, session, message)
    return fresh_id or last_id
