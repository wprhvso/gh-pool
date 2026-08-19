from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry.metrics import get_meter
from yaol import attached, capture, fail, record_exception, span

from pool_runners.api import ScaleSet
from pool_runners.budget import REST
from pool_runners.config import (
    ALIVE,
    DRAIN_POLL,
    FLEET_INTERVAL,
    JOB_AVAILABLE,
    JOB_COMPLETED,
    JOIN_WAIT,
    MAX_LOOP_FAILURES,
    QUEUE_MESSAGE_TYPE,
    RATE_WAIT_CAP,
    SESSION_STATUSES,
    STATS_INTERVAL,
    SUBMIT_WORKERS,
    TOKEN_SKEW,
    WORKER_STALE,
)
from pool_runners.errors import HttpError, RateLimited, RunnerError
from pool_runners.fleet import Fleet
from pool_runners.gh import delete_runner, preflight, release_version, runners
from pool_runners.http import STOP, backoff
from pool_runners.models import Stats, to_int
from pool_runners.pool import Pool

if TYPE_CHECKING:
    from types import FrameType

    from pool_runners.config import Server, Target
    from pool_runners.models import Session

log = logging.getLogger("runners")

_EXIT_INTERRUPTED = 130
_UNAUTHORIZED = 401
_NOT_FOUND = 404
_TIMEOUT_GRACE = 480.0

CODE = (Path(__file__).resolve().parent / "agent.py").read_text(encoding="utf-8")
FORCE = threading.Event()

_meter = get_meter("pool_runners")
JOBS_ACQUIRED = _meter.create_counter(
    "pool_runners.jobs_acquired",
    unit="{job}",
    description="job'ы, забранные из очереди GitHub",
)
RUNNERS_LAUNCHED = _meter.create_counter(
    "pool_runners.runners_launched",
    unit="{runner}",
    description="попытки отправить раннера в пул",
)
FLEET_SIZE = _meter.create_gauge(
    "pool_runners.fleet_size",
    unit="{runner}",
    description="раннеры, которые контроллер считает своими",
)
LAUNCH_DURATION = _meter.create_histogram(
    "pool_runners.launch_duration",
    unit="s",
    description="время раскидывания партии раннеров по пулу",
)


class Latest:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._stats: Stats = Stats()

    def set(self, stats: Stats) -> Stats:
        with self._lock:
            self._stats = stats
            return stats

    def get(self) -> Stats:
        with self._lock:
            return self._stats

    def took(self, taken: int) -> Stats:
        with self._lock:
            self._stats = replace(self._stats, assigned=self._stats.assigned + taken)
            return self._stats


@dataclass(frozen=True)
class Ctx:
    api: ScaleSet
    pool: Pool
    target: Target
    scale_set_id: int
    fleet: Fleet
    latest: Latest
    scaling: threading.Lock
    closing: threading.Event

    @property
    def slug(self) -> str:
        return self.target.slug


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


def _read_jobs(raw_body: str) -> tuple[list[int], list[str]]:
    try:
        items = json.loads(raw_body or "[]")
    except json.JSONDecodeError:
        log.debug("не разобрал тело сообщения")
        return [], []
    if not isinstance(items, list):
        return [], []

    offered: list[int] = []
    retired: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("messageType") or "")
        request_id = item.get("runnerRequestId")
        log.info(
            "  %s: %s (run %s, request %s)",
            kind or "?",
            item.get("jobDisplayName"),
            item.get("workflowRunId"),
            request_id,
        )
        offer = to_int(request_id)
        if offer and JOB_AVAILABLE in kind:
            offered.append(offer)
        if JOB_COMPLETED in kind and item.get("runnerName"):
            retired.append(str(item["runnerName"]))
    return offered, retired


def _launch(ctx: Ctx, version: str) -> bool:
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
                timeout=ctx.target.lifetime + _TIMEOUT_GRACE,
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


def _launch_many(ctx: Ctx, count: int, version: str) -> int:
    if count == 1:
        return int(_launch(ctx, version))
    stumbled = threading.Event()
    parent = capture()

    def once() -> bool:
        if stumbled.is_set():
            return False
        with attached(parent):
            if _launch(ctx, version):
                return True
        stumbled.set()
        return False

    with ThreadPoolExecutor(
        max_workers=min(count, SUBMIT_WORKERS),
        thread_name_prefix="submit",
    ) as pool:
        sending = [pool.submit(once) for _ in range(count)]
        return sum(int(item.result()) for item in sending)


def _cancel(ctx: Ctx, task_id: str) -> bool:
    try:
        ctx.pool.cancel(task_id)
    except HttpError as exc:
        if exc.status != _NOT_FOUND:
            log.debug("%s: задача %s не отменилась: %s", ctx.slug, task_id, exc)
            return False
    except RunnerError as exc:
        log.debug("%s: задача %s не отменилась: %s", ctx.slug, task_id, exc)
        return False
    gone = ctx.fleet.drop(task_id)
    if gone is not None and gone.runner_id:
        ctx.api.forget(gone.runner_id)
    return True


def _shrink(ctx: Ctx, surplus: int) -> int:
    dropped = sum(_cancel(ctx, slot.task_id) for slot in ctx.fleet.spare()[:surplus])
    if dropped:
        log.info("%s: неуехавших раннеров снял: %s", ctx.slug, dropped)
    return dropped


def _scale(ctx: Ctx, stats: Stats, note: str, *, shrink: bool = False) -> None:
    with span("runners.scale", {"repo": ctx.slug, "scale.note": note}) as active:
        try:
            version = ctx.target.version or release_version()
        except RunnerError as exc:
            log.warning("%s: не выяснил версию раннера: %s", ctx.slug, exc)
            record_exception(exc)
            version = ""

        with ctx.scaling:
            if ctx.closing.is_set():
                return
            want = min(ctx.target.jobs, stats.assigned)
            have = ctx.fleet.size()
            need = want - have

            active.set_attributes({"scale.want": want, "scale.have": have})
            log.info("%s | %s: want=%s have=%s need=%s", stats, note, want, have, need)
            if need < 0:
                if shrink:
                    _shrink(ctx, -need)
                return
            if not need or not version:
                return

            started = time.monotonic()
            sent = _launch_many(ctx, need, version)
            spent = time.monotonic() - started
            LAUNCH_DURATION.record(spent, {"repo": ctx.slug})
            active.set_attribute("scale.launched", sent)
            if sent < need:
                fail(f"раннеров уехало {sent} из {need}")
            log.info(
                "%s: раскидал раннеров %s/%s за %.1f с",
                ctx.slug,
                sent,
                need,
                spent,
            )


def _acquire(ctx: Ctx, session: Session, ids: list[int]) -> int:
    if not ids:
        return 0
    try:
        taken = ctx.api.acquire(ctx.scale_set_id, session, ids)
    except HttpError as exc:
        if exc.status == _UNAUTHORIZED:
            raise
        log.warning("%s: не забрал job'ы %s: %s", ctx.slug, ids, exc)
        record_exception(exc)
        return 0
    JOBS_ACQUIRED.add(taken, {"repo": ctx.slug})
    log.info("%s: забрал job'ов: %s из %s", ctx.slug, taken, len(ids))
    return taken


def _handle(ctx: Ctx, session: Session, message: dict[str, Any]) -> None:
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
        offered, retired = _read_jobs(str(message.get("body") or ""))
        active.set_attributes(
            {"queue.offered": len(offered), "queue.retired": len(retired)}
        )
        for name in retired:
            if ctx.fleet.spend(name):
                log.info("%s: раннер %s своё отработал", ctx.slug, name)
        taken = _acquire(ctx, session, offered)
        if taken:
            stats = ctx.latest.took(taken)
        active.set_attribute("queue.acquired", taken)
        _scale(ctx, stats, "сообщение")


def _pick_up(ctx: Ctx, session: Session, stats: Stats, note: str) -> None:
    if stats.available or stats.assigned:
        ids = [
            found
            for job in ctx.api.acquirable(ctx.scale_set_id)
            if (found := to_int(job.get("runnerRequestId")))
        ]
        taken = _acquire(ctx, session, ids)
        if taken:
            stats = ctx.latest.took(taken)
    _scale(ctx, stats, note)


def _ack(ctx: Ctx, session: Session, message: dict[str, Any]) -> None:
    message_id = to_int(message.get("messageId"))
    if not message_id:
        return
    try:
        ctx.api.ack(session, message_id)
    except RunnerError as exc:
        log.warning("%s: не подтвердил сообщение %s: %s", ctx.slug, message_id, exc)


def _adopt(ctx: Ctx) -> int:
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


def _held(ctx: Ctx) -> set[str]:
    try:
        rows = ctx.pool.workers()
    except RunnerError as exc:
        log.debug("%s: не спросил воркеров: %s", ctx.slug, exc)
        return set()
    return {str(row.get("task_id")) for row in rows if row.get("task_id")}


def _why(ctx: Ctx, task_id: str, state: dict[str, Any]) -> str:
    error = str(state.get("error") or "").strip()
    try:
        tail = ctx.pool.tail(task_id, 600).strip().splitlines()
    except RunnerError:
        tail = []
    return f"{error or 'без причины'}: {tail[-1] if tail else ''}"[:300]


def _reconcile(ctx: Ctx) -> None:
    held = _held(ctx) if ctx.fleet.tracking() else set()
    slots = ctx.fleet.slots()
    blind = 0
    for slot in slots:
        if slot.lived() > ctx.target.lifetime + _TIMEOUT_GRACE:
            log.warning(
                "%s: раннер %s живёт дольше отпущенного, снимаю", ctx.slug, slot.name
            )
            _cancel(ctx, slot.task_id)
            ctx.fleet.drop(slot.task_id)
            continue
        try:
            state = ctx.pool.state(slot.task_id)
        except HttpError as exc:
            if exc.status != _NOT_FOUND:
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
        if status not in ALIVE:
            ctx.fleet.drop(slot.task_id)
            log.info("%s: раннер %s отработал: %s", ctx.slug, slot.name, status)
            if status in ("failed", "lost"):
                log.warning(
                    "%s: %s — %s", ctx.slug, slot.name, _why(ctx, slot.task_id, state)
                )
            continue

        ctx.fleet.mark(slot.task_id, status)
        if (
            status == "running"
            and held
            and slot.task_id not in held
            and slot.age() > WORKER_STALE
        ):
            log.warning(
                "%s: воркер с раннером %s пропал, снимаю задачу", ctx.slug, slot.name
            )
            _cancel(ctx, slot.task_id)

    if blind:
        log.warning(
            "%s: пул не ответил по %s слотам из %s, флот не сверен",
            ctx.slug,
            blind,
            len(slots),
        )


def _reconcile_loop(ctx: Ctx, stop: threading.Event) -> None:
    asked = 0.0
    while not stop.wait(FLEET_INTERVAL):
        try:
            _reconcile(ctx)
            FLEET_SIZE.set(ctx.fleet.size(), {"repo": ctx.slug})
            if time.monotonic() - asked > STATS_INTERVAL:
                ctx.latest.set(ctx.api.statistics(ctx.scale_set_id))
                asked = time.monotonic()
            _scale(ctx, ctx.latest.get(), "сверка", shrink=True)
        except RateLimited as exc:
            log.debug("сверка ждёт сброса лимита: %s", exc)
        except RunnerError as exc:
            log.debug("%s: не сверил флот: %s", ctx.slug, exc)
        except Exception:
            log.exception("сверка флота сорвалась")


def _fresh(ctx: Ctx, session: Session) -> Session:
    if time.time() < session.queue_token_exp - TOKEN_SKEW:
        return session
    log.debug("токен очереди на исходе, обновляю заранее")
    session = ctx.api.refresh(ctx.scale_set_id, session)
    ctx.latest.set(session.stats)
    return session


def _recover(
    ctx: Ctx, session: Session, owner: str, status: int
) -> tuple[Session, int]:
    if status == _UNAUTHORIZED:
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


def _hold(exc: RateLimited, stop: threading.Event) -> None:
    waiting = min(max(exc.retry_in, 1.0), RATE_WAIT_CAP)
    log.warning("лимит REST выбран, жду %.0f с — %s", waiting, REST.state())
    stop.wait(waiting)


def _tick(ctx: Ctx, session: Session, last_id: int, stop: threading.Event) -> int:
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
        _pick_up(ctx, session, ctx.latest.get(), "тишина")
        return last_id

    fresh_id = to_int(message.get("messageId"))
    if fresh_id and fresh_id <= last_id:
        log.debug("сообщение %s уже обработано, подтверждаю снова", fresh_id)
        _ack(ctx, session, message)
        return last_id

    _handle(ctx, session, message)
    _ack(ctx, session, message)
    return fresh_id or last_id


def _drain(ctx: Ctx) -> Stats:
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


def _pause(ctx: Ctx, session: Session | None) -> None:
    if session is None:
        return
    try:
        ctx.api.close(ctx.scale_set_id, session)
    except RunnerError as exc:
        log.warning("сессия не закрылась: %s", exc)


def _cleanup(ctx: Ctx, session: Session | None) -> None:
    ctx.closing.set()
    _pause(ctx, session)
    stats = _drain(ctx)
    busy = bool(stats.running) and not FORCE.is_set()

    doomed = ctx.fleet.spare() if busy else ctx.fleet.slots()
    cancelled = sum(_cancel(ctx, slot.task_id) for slot in doomed)

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
