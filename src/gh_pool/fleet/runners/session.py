from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from gh_pool.fleet.runners.budget import REST
from gh_pool.fleet.runners.config import RATE_WAIT_CAP, TOKEN_SKEW
from gh_pool.fleet.runners.errors import RunnerError

if TYPE_CHECKING:
    from gh_pool.fleet.runners.errors import RateLimited
    from gh_pool.fleet.runners.models import Session
    from gh_pool.fleet.runners.state import Ctx

log = logging.getLogger(__name__)

UNAUTHORIZED = 401


def fresh(ctx: Ctx, session: Session) -> Session:
    if time.time() < session.queue_token_exp - TOKEN_SKEW:
        return session
    log.debug("токен очереди на исходе, обновляю заранее")
    session = ctx.api.refresh(ctx.scale_set_id, session)
    ctx.latest.set(session.stats)
    return session


def recover(ctx: Ctx, session: Session, owner: str, status: int) -> tuple[Session, int]:
    if status == UNAUTHORIZED:
        try:
            return ctx.api.refresh(ctx.scale_set_id, session), -1
        except RunnerError as exc:
            log.info("токен очереди не обновился: %s", exc)
    log.info("сессия недействительна (%s), пересоздаю", status)
    try:
        return ctx.api.reopen(ctx.scale_set_id, session, owner), 0
    except RunnerError as exc:
        log.warning("сессия не пересоздалась: %s", exc)
        return session, -1


def hold(exc: RateLimited, stop: threading.Event) -> None:
    waiting = min(max(exc.retry_in, 1.0), RATE_WAIT_CAP)
    log.warning("лимит REST выбран, жду %.0f с — %s", waiting, REST.state())
    stop.wait(waiting)
