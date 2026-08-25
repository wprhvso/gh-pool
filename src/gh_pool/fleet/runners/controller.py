from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING

from gh_pool.fleet.runners.api import ScaleSet
from gh_pool.fleet.runners.config import (
    JOIN_WAIT,
    MAX_LOOP_FAILURES,
    SESSION_STATUSES,
)
from gh_pool.fleet.runners.errors import HttpError, RateLimited, RunnerError
from gh_pool.fleet.runners.fleet import Fleet
from gh_pool.fleet.runners.gh import preflight
from gh_pool.fleet.runners.http import STOP, backoff
from gh_pool.fleet.runners.pool import Pool
from gh_pool.fleet.runners.queue import pick_up as _pick_up
from gh_pool.fleet.runners.queue import tick as _tick
from gh_pool.fleet.runners.reconcile import adopt as _adopt
from gh_pool.fleet.runners.reconcile import reconcile_loop as _reconcile_loop
from gh_pool.fleet.runners.session import fresh as _fresh
from gh_pool.fleet.runners.session import hold as _hold
from gh_pool.fleet.runners.session import recover as _recover
from gh_pool.fleet.runners.state import FORCE, Ctx, Latest
from gh_pool.fleet.runners.teardown import cleanup as _cleanup
from gh_pool.fleet.runners.teardown import pause as _pause

if TYPE_CHECKING:
    from types import FrameType

    from gh_pool.fleet.runners.config import Server, Target
    from gh_pool.fleet.runners.models import Session

log = logging.getLogger(__name__)

_EXIT_INTERRUPTED = 130


def install_stop_handler() -> threading.Event:
    stop = STOP

    def handler(signum: int, _frame: FrameType | None) -> None:
        if FORCE.is_set():
            log.warning("третий сигнал, выхожу немедленно")
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(_EXIT_INTERRUPTED)
        if stop.is_set():
            log.warning("второй сигнал, обрываю job'ы на полуслове")
            FORCE.set()
            return
        log.info("сигнал %s — доработаю итерацию и уберу за собой", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)
    return stop


def _start(target: Target, server: Server) -> Ctx:
    preflight(target)

    api = ScaleSet(target)
    scale_set, _created = api.ensure(target.label)
    pool = Pool(server)
    ctx = Ctx(
        api=api,
        pool=pool,
        target=target,
        scale_set_id=int(scale_set["id"]),
        fleet=Fleet(),
        latest=Latest(),
        scaling=threading.Lock(),
        closing=threading.Event(),
    )

    log.info(
        "%s: пул=%s метка=%r scale-set=%s max=%s idle=%.0fс жизнь=%.0fс",
        target.slug,
        pool.url,
        target.label,
        ctx.scale_set_id,
        target.jobs,
        target.idle,
        target.lifetime,
    )
    _adopt(ctx)
    return ctx


def run(target: Target, server: Server, stop: threading.Event) -> int:
    ctx = _start(target, server)
    owner = f"pool-runners-{threading.get_ident()}"

    session: Session | None = None
    last_id = 0
    failures = 0
    done = threading.Event()
    watcher = threading.Thread(
        target=_reconcile_loop,
        args=(ctx, done),
        name=f"{target.slug}~флот",
        daemon=True,
    )

    try:
        session = ctx.api.reopen(ctx.scale_set_id, None, owner)
        ctx.latest.set(session.stats)
        watcher.start()
        _pick_up(ctx, session, session.stats, "старт")

        while not stop.is_set():
            try:
                session = _fresh(ctx, session)
                last_id = _tick(ctx, session, last_id, stop)
                failures = 0

            except RateLimited as exc:
                failures = 0
                _hold(exc, stop)

            except HttpError as exc:
                if exc.status not in SESSION_STATUSES:
                    raise
                failures += 1
                if failures >= MAX_LOOP_FAILURES:
                    raise
                session, reset = _recover(ctx, session, owner, exc.status)
                if reset >= 0:
                    last_id = reset
                if failures > 1:
                    stop.wait(backoff(failures))

            except RunnerError as exc:
                failures += 1
                if failures >= MAX_LOOP_FAILURES:
                    raise
                log.warning(
                    "%s: итерация не удалась (%s/%s): %s",
                    ctx.slug,
                    failures,
                    MAX_LOOP_FAILURES,
                    exc,
                )
                stop.wait(backoff(failures))
    finally:
        done.set()
        ctx.closing.set()
        if watcher.ident is not None:
            watcher.join(JOIN_WAIT)
        if stop.is_set():
            _cleanup(ctx, session)
        else:
            log.warning("%s: перезапуск — раннеров и scale set не трогаю", ctx.slug)
            _pause(ctx, session)

    return 0
