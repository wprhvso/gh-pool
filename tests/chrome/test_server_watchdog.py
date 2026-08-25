import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest

from gh_pool.core.config import settings
from gh_pool.core.events import QUEUE_SIZE, SubscriberOverflow, Subscription
from gh_pool.protocol import CloseReason, CommandError, ErrorCode, Event, SessionReady
from gh_pool.server.watchdog import Watchdog

SESSION = uuid4()


class FakeSessions:
    def __init__(self) -> None:
        self.expired: list[dict[str, Any]] = []
        self.dead: list[UUID] = []
        self.completed: list[tuple[UUID, UUID, CommandError | None]] = []
        self.cancelled: list[tuple[UUID, UUID]] = []
        self.finished: list[tuple[UUID, CloseReason]] = []
        self.asked_with: list[tuple[float, float]] = []
        self.ticks = 0
        self.fail_once = False

    async def expired_commands(self) -> list[dict[str, Any]]:
        self.ticks += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("the database went away")
        taken, self.expired = self.expired, []
        return taken

    async def dead_candidates(
        self, heartbeat_timeout: float, ready_timeout: float
    ) -> list[UUID]:
        self.asked_with.append((heartbeat_timeout, ready_timeout))
        taken, self.dead = self.dead, []
        return taken

    async def complete(
        self,
        session_id: UUID,
        command_id: UUID,
        _result: object,
        error: CommandError | None,
    ) -> None:
        self.completed.append((session_id, command_id, error))

    def request_cancel(self, session_id: UUID, command_id: UUID) -> None:
        self.cancelled.append((session_id, command_id))

    async def finish(self, session_id: UUID, reason: CloseReason) -> None:
        self.finished.append((session_id, reason))


def _watchdog(sessions: FakeSessions) -> Watchdog:
    return Watchdog(sessions)


async def _until(condition, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise TimeoutError(f"{what} did not happen in {timeout}s")
        await asyncio.sleep(0.01)


async def test_a_command_that_outstayed_its_timeout_is_failed_and_dropped():
    sessions = FakeSessions()
    command_id = uuid4()
    sessions.expired = [{"id": command_id, "session_id": SESSION}]

    await _watchdog(sessions)._tick()

    assert sessions.completed[0][:2] == (SESSION, command_id)
    error = sessions.completed[0][2]
    assert error is not None
    assert error.code is ErrorCode.TIMEOUT
    assert sessions.cancelled == [(SESSION, command_id)]


async def test_a_session_nobody_is_keeping_alive_is_given_up_on():
    sessions = FakeSessions()
    sessions.dead = [SESSION]

    await _watchdog(sessions)._tick()

    assert sessions.finished == [(SESSION, CloseReason.DEAD)]


async def test_the_fuses_are_the_ones_the_operator_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "heartbeat_timeout", 11.0)
    monkeypatch.setattr(settings, "ready_timeout", 22.0)
    sessions = FakeSessions()

    await _watchdog(sessions)._tick()

    assert sessions.asked_with == [(11.0, 22.0)]


async def test_a_quiet_tick_touches_nothing():
    sessions = FakeSessions()

    await _watchdog(sessions)._tick()

    assert sessions.completed == []
    assert sessions.finished == []


async def test_the_watchdog_keeps_ticking(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "watchdog_interval", 0.01)
    sessions = FakeSessions()
    watchdog = _watchdog(sessions)

    await watchdog.start()
    try:
        await _until(lambda: sessions.ticks >= 3, "three ticks")
    finally:
        await watchdog.stop()


async def test_a_tick_that_failed_does_not_stop_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "watchdog_interval", 0.01)
    sessions = FakeSessions()
    sessions.fail_once = True
    watchdog = _watchdog(sessions)

    await watchdog.start()
    try:
        await _until(lambda: sessions.ticks >= 3, "the watchdog to carry on")
    finally:
        await watchdog.stop()


async def test_stopping_a_watchdog_that_never_started_is_not_an_error():
    await _watchdog(FakeSessions()).stop()


def _event(seq: int) -> Event:
    return Event(seq=seq, data=SessionReady(state_stale=False))


async def test_a_subscriber_is_given_what_it_was_offered_in_order():
    subscription = Subscription()

    subscription.offer(_event(1))
    subscription.offer(_event(2))

    assert (await subscription.get()).seq == 1
    assert (await subscription.get()).seq == 2


async def test_a_subscriber_that_fell_too_far_behind_is_told_rather_than_starved():
    subscription = Subscription()
    for seq in range(QUEUE_SIZE + 1):
        subscription.offer(_event(seq + 1))

    with pytest.raises(SubscriberOverflow):
        await subscription.get()


async def test_a_subscriber_that_overflowed_stays_overflowed():
    subscription = Subscription()
    for seq in range(QUEUE_SIZE + 1):
        subscription.offer(_event(seq + 1))
    subscription.offer(_event(9999))

    with pytest.raises(SubscriberOverflow):
        await subscription.get()


async def test_a_subscriber_with_nothing_to_read_waits():
    subscription = Subscription()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await subscription.get()
