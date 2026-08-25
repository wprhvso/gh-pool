from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from gh_pool.fleet.runners.budget import REST
from gh_pool.fleet.runners.config import DRAIN_POLL
from gh_pool.fleet.runners.errors import RunnerError
from gh_pool.fleet.runners.gh import delete_runner, runners
from gh_pool.fleet.runners.provision import cancel
from gh_pool.fleet.runners.state import FORCE

if TYPE_CHECKING:
    from gh_pool.fleet.runners.models import Session, Stats
    from gh_pool.fleet.runners.state import Ctx

log = logging.getLogger(__name__)


def drain(ctx: Ctx) -> Stats:
    stats = ctx.latest.get()
    if ctx.target.drain <= 0:
        return stats
    deadline = time.monotonic() + ctx.target.drain
    while not FORCE.is_set():
        try:
            stats = ctx.latest.set(ctx.api.statistics(ctx.scale_set_id))
        except RunnerError as exc:
            log.debug("%s: не спросил статистику при сливе: %s", ctx.slug, exc)
            return stats
        if not stats.running or time.monotonic() > deadline:
            return stats
        log.info(
            "%s: жду, пока доработают job'ы: running=%s, осталось %.0f с",
            ctx.slug,
            stats.running,
            deadline - time.monotonic(),
        )
        FORCE.wait(DRAIN_POLL)
    return stats


def pause(ctx: Ctx, session: Session | None) -> None:
    if session is None:
        return
    try:
        ctx.api.close(ctx.scale_set_id, session)
    except RunnerError as exc:
        log.warning("сессия не закрылась: %s", exc)


def cleanup(ctx: Ctx, session: Session | None) -> None:
    ctx.closing.set()
    pause(ctx, session)
    stats = drain(ctx)
    busy = bool(stats.running) and not FORCE.is_set()

    doomed = ctx.fleet.spare() if busy else ctx.fleet.slots()
    cancelled = sum(cancel(ctx, slot.task_id) for slot in doomed)

    if busy:
        log.warning(
            "%s: job'ов ещё в работе %s — оставляю scale set, раннеры доживут сами",
            ctx.slug,
            stats.running,
        )
        log.info("%s: убрал за собой: задач снято=%s", ctx.slug, cancelled)
        return

    removed_set = False
    try:
        ctx.api.drop(ctx.scale_set_id)
        removed_set = True
    except RunnerError as exc:
        log.warning("scale set не удалён: %s", exc)

    removed = 0
    waiting = REST.shut()
    if waiting:
        log.warning("лимит REST закрыт ещё %.0f с — раннеров не подчищаю", waiting)
    else:
        try:
            removed = sum(
                delete_runner(ctx.target, int(item["id"]))
                for item in runners(ctx.target)
            )
        except RunnerError as exc:
            log.warning("не перечислил раннеров: %s", exc)

    log.info(
        "%s: убрал за собой: задач снято=%s scale-set=%s раннеров удалено=%s",
        ctx.slug,
        cancelled,
        removed_set,
        removed,
    )
