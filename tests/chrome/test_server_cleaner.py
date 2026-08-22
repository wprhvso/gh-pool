import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from gh_pool.server import storage
from gh_pool.server.cleaner import DAY, Cleaner
from gh_pool.server.config import settings

KIB = 1 << 10
HOUR = 3600.0


class FakeSessions:
    def __init__(self) -> None:
        self.closed: dict[UUID, float] = {}
        self.forgotten: list[UUID] = []
        self.asked: list[float] = []
        self.ticks = 0
        self.fail_once = False

    def closed_ago(self, seconds: float) -> UUID:
        session_id = uuid4()
        self.closed[session_id] = seconds
        return session_id

    async def closed_before(self, max_age: float) -> list[UUID]:
        self.ticks += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("the database went away")
        self.asked.append(max_age)
        older = [item for item in self.closed.items() if item[1] > max_age]
        return [session_id for session_id, _ in sorted(older, key=_by_age)]

    async def forget(self, session_id: UUID) -> None:
        self.forgotten.append(session_id)
        del self.closed[session_id]


def _by_age(item: tuple[UUID, float]) -> float:
    return -item[1]


def _cleaner(sessions: FakeSessions) -> Cleaner:
    return Cleaner(sessions)  # pyright: ignore[reportArgumentType]


async def _until(condition, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise TimeoutError(f"{what} did not happen in {timeout}s")
        await asyncio.sleep(0.01)


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "storage", tmp_path)
    monkeypatch.setattr(settings, "cleanup_max_days", 7.0)
    monkeypatch.setattr(settings, "cleanup_max_bytes", 1 << 30)
    monkeypatch.setattr(settings, "runner_grace", 300.0)
    storage.ensure_dirs()
    return tmp_path


def _record(session_id: UUID, kib: int) -> None:
    segments = storage.segments_dir(session_id)
    segments.mkdir(parents=True, exist_ok=True)
    (segments / "1.m4s").write_bytes(b"x" * (kib * KIB))
    downloads = storage.downloads_dir(session_id)
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / "report.pdf").write_bytes(b"y" * KIB)
    uploads = storage.files_dir(session_id)
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "payload.bin").write_bytes(b"z" * KIB)


def _left(session_id: UUID) -> bool:
    return (
        storage.session_dir(session_id).exists()
        or storage.files_dir(session_id).exists()
    )


async def test_a_session_closed_long_enough_ago_takes_its_recording_with_it(
    storage_root: Path,
):
    sessions = FakeSessions()
    session_id = sessions.closed_ago(8 * DAY)
    _record(session_id, kib=16)

    await _cleaner(sessions)._tick()

    assert sessions.forgotten == [session_id]
    assert not _left(session_id)


async def test_a_session_still_running_is_left_where_it_is(storage_root: Path):
    running = uuid4()
    _record(running, kib=16)
    sessions = FakeSessions()

    await _cleaner(sessions)._tick()

    assert sessions.forgotten == []
    assert _left(running)


async def test_a_session_closed_only_recently_is_left_where_it_is(storage_root: Path):
    sessions = FakeSessions()
    session_id = sessions.closed_ago(HOUR)
    _record(session_id, kib=16)

    await _cleaner(sessions)._tick()

    assert sessions.forgotten == []
    assert _left(session_id)


async def test_the_age_the_operator_configured_is_the_one_asked_for(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_max_days", 3.0)
    sessions = FakeSessions()

    await _cleaner(sessions)._tick()

    assert sessions.asked == [3.0 * DAY]


async def test_a_profile_archive_is_not_something_the_cleaner_may_touch(
    storage_root: Path,
):
    archive = storage.profile_path("aistudio-one")
    archive.write_bytes(b"a google account somebody signed in by hand")
    sessions = FakeSessions()
    session_id = sessions.closed_ago(8 * DAY)
    _record(session_id, kib=16)

    await _cleaner(sessions)._tick()

    assert not _left(session_id)
    assert archive.read_bytes() == b"a google account somebody signed in by hand"


async def test_storage_over_the_limit_gives_up_its_oldest_sessions_first(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_max_bytes", 40 * KIB)
    sessions = FakeSessions()
    oldest = sessions.closed_ago(3 * HOUR)
    older = sessions.closed_ago(2 * HOUR)
    newest = sessions.closed_ago(HOUR)
    for session_id in (oldest, older, newest):
        _record(session_id, kib=16)

    await _cleaner(sessions)._tick()

    assert sessions.forgotten == [oldest]
    assert not _left(oldest)
    assert _left(older)
    assert _left(newest)


async def test_storage_within_the_limit_loses_nothing(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_max_bytes", 1 << 20)
    sessions = FakeSessions()
    session_id = sessions.closed_ago(HOUR)
    _record(session_id, kib=16)

    await _cleaner(sessions)._tick()

    assert sessions.forgotten == []
    assert _left(session_id)


async def test_a_session_whose_runner_may_still_be_talking_is_not_evicted_for_room(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_max_bytes", KIB)
    sessions = FakeSessions()
    session_id = sessions.closed_ago(10.0)
    _record(session_id, kib=16)

    await _cleaner(sessions)._tick()

    assert sessions.forgotten == []
    assert _left(session_id)


async def test_storage_that_only_live_sessions_fill_is_reported_not_hidden(
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setattr(settings, "cleanup_max_bytes", KIB)
    running = uuid4()
    _record(running, kib=64)
    sessions = FakeSessions()

    await _cleaner(sessions)._tick()

    assert _left(running)
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "allowed" in caplog.text


async def test_the_first_pass_waits_before_competing_with_the_start_up(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "cleanup_delay", 30.0)
    monkeypatch.setattr(settings, "cleanup_interval", 0.01)
    sessions = FakeSessions()
    cleaner = _cleaner(sessions)

    await cleaner.start()
    try:
        await asyncio.sleep(0.05)
        assert sessions.ticks == 0
    finally:
        await cleaner.stop()


async def test_the_cleaner_keeps_going_round(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_delay", 0.0)
    monkeypatch.setattr(settings, "cleanup_interval", 0.01)
    sessions = FakeSessions()
    cleaner = _cleaner(sessions)

    await cleaner.start()
    try:
        await _until(lambda: sessions.ticks >= 3, "three passes")
    finally:
        await cleaner.stop()


async def test_a_pass_that_failed_does_not_stop_the_cleaner(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_delay", 0.0)
    monkeypatch.setattr(settings, "cleanup_interval", 0.01)
    sessions = FakeSessions()
    sessions.fail_once = True
    cleaner = _cleaner(sessions)

    await cleaner.start()
    try:
        await _until(lambda: sessions.ticks >= 3, "the cleaner to carry on")
    finally:
        await cleaner.stop()


async def test_stopping_a_cleaner_that_never_started_is_not_an_error():
    await _cleaner(FakeSessions()).stop()
