from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from gh_pool.fleet.runners import controller as ctrl
from gh_pool.fleet.runners import policy as policy_mod
from gh_pool.fleet.runners import reconcile as reconcile_mod
from gh_pool.fleet.runners import teardown as teardown_mod
from gh_pool.fleet.runners.config import Server, Target
from gh_pool.fleet.runners.models import Stats
from tests.fleet.fake import FakePool, FakeScaleSet, job
from tests.fleet.test_agent import JOB, _tarball

VERSION = "2.999.0"


@pytest.fixture
def stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    room = tmp_path / "cache"
    room.mkdir()
    monkeypatch.setenv("RUNNERS_CACHE", str(room))
    from gh_pool.fleet.runners import agent as agent_mod

    _tarball(room / f"actions-runner-linux-{agent_mod.arch()}-{VERSION}.tar.gz", JOB)
    monkeypatch.setattr(reconcile_mod, "FLEET_INTERVAL", 0.2)
    monkeypatch.setattr(policy_mod, "release_version", lambda: VERSION)
    monkeypatch.setattr(ctrl, "preflight", lambda _target: {"private": True})
    monkeypatch.setattr(teardown_mod, "runners", lambda _target: [])
    return room


def _wait(check, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_a_queued_job_turns_into_a_runner_on_the_pool(
    stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Target(slug="owner/app", token="ghp", jobs=4, idle=10, lifetime=60)
    api = FakeScaleSet(target)
    pool = FakePool()
    monkeypatch.setattr(ctrl, "ScaleSet", lambda _target: api)
    monkeypatch.setattr(ctrl, "Pool", lambda _server: pool)

    api.offer(job(1), stats=Stats(available=1))

    stop = threading.Event()
    loop = threading.Thread(
        target=ctrl.run, args=(target, Server("https://pool", "t"), stop)
    )
    loop.start()
    try:
        assert _wait(lambda: bool(pool.done())), "раннер так и не отработал"
        assert _wait(
            lambda: (
                pool.tasks and all(t.status != "pending" for t in pool.tasks.values())
            )
        )
    finally:
        stop.set()
        loop.join(30)

    assert not loop.is_alive()

    task = pool.done()[0]
    assert task.kwargs["version"] == VERSION
    assert task.kwargs["jit"] in api.jits[0] or task.kwargs["jit"].endswith(api.jits[0])
    assert task.outcome["jobs"] == 1
    assert task.outcome["results"] == ["Succeeded"]

    kinds = [event["kind"] for event in task.events]
    assert "runner" in kinds
    assert "job" in kinds

    assert api.acquired == [1]
    assert api.acked == [1]
    assert api.closed
    assert api.dropped == [42]


def test_the_fleet_empties_itself_after_the_job(
    stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Target(
        slug="owner/app", token="ghp", jobs=2, idle=10, lifetime=60, drain=0
    )
    api = FakeScaleSet(target)
    pool = FakePool()
    monkeypatch.setattr(ctrl, "ScaleSet", lambda _target: api)
    monkeypatch.setattr(ctrl, "Pool", lambda _server: pool)

    api.offer(job(5), stats=Stats(available=1))

    stop = threading.Event()
    ctx_seen: list[ctrl.Ctx] = []
    start = ctrl._start
    monkeypatch.setattr(
        ctrl, "_start", lambda t, s: ctx_seen.append(start(t, s)) or ctx_seen[0]
    )

    loop = threading.Thread(
        target=ctrl.run, args=(target, Server("https://pool", "t"), stop)
    )
    loop.start()
    try:
        assert _wait(lambda: bool(pool.done()))
        assert _wait(lambda: ctx_seen and ctx_seen[0].fleet.size() == 0), (
            "слот не освободился"
        )
    finally:
        stop.set()
        loop.join(30)

    assert len(pool.tasks) == 1


def test_a_batch_of_jobs_fills_and_empties_the_fleet(
    stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Target(
        slug="owner/app", token="ghp", jobs=3, idle=10, lifetime=60, drain=0
    )
    api = FakeScaleSet(target)
    pool = FakePool()
    monkeypatch.setattr(ctrl, "ScaleSet", lambda _target: api)
    monkeypatch.setattr(ctrl, "Pool", lambda _server: pool)

    deleted: list[int] = []

    def remember(_target: Target, runner_id: int) -> bool:
        deleted.append(runner_id)
        return True

    monkeypatch.setattr(teardown_mod, "runners", lambda _target: [{"id": 7}, {"id": 8}])
    monkeypatch.setattr(teardown_mod, "delete_runner", remember)

    api.offer(job(1), job(2), job(3), stats=Stats(available=3))

    stop = threading.Event()
    loop = threading.Thread(
        target=ctrl.run, args=(target, Server("https://pool", "t"), stop)
    )
    loop.start()
    try:
        assert _wait(lambda: len(pool.done()) == 3), "не все раннеры отработали"
    finally:
        stop.set()
        loop.join(30)

    assert not loop.is_alive()
    assert sorted(api.acquired) == [1, 2, 3]
    assert len(api.jits) >= 3
    assert api.dropped == [42]
    assert deleted == [7, 8]
    assert all(task.outcome["jobs"] == 1 for task in pool.done())
