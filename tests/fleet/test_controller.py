from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest
from gh_pool.fleet.runners import controller as ctrl
from gh_pool.fleet.runners.config import Server, Target
from gh_pool.fleet.runners.errors import RunnerError
from gh_pool.fleet.runners.fleet import Fleet
from gh_pool.fleet.runners.models import Stats
from tests.fleet.fake import FakePool, FakeScaleSet, job, message, refused


def _ctx(
    target: Target | None = None,
    pool: FakePool | None = None,
    api: FakeScaleSet | None = None,
) -> ctrl.Ctx:
    target = target or Target(slug="owner/app", token="ghp", jobs=4)
    return ctrl.Ctx(
        api=api or FakeScaleSet(target),  # pyright: ignore[reportArgumentType]
        pool=pool or FakePool(run=False),  # pyright: ignore[reportArgumentType]
        target=target,
        scale_set_id=42,
        fleet=Fleet(),
        latest=ctrl.Latest(),
        scaling=threading.Lock(),
        closing=threading.Event(),
    )


@pytest.fixture(autouse=True)
def version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctrl, "release_version", lambda: "2.999.0")


def test_only_offered_jobs_are_collected() -> None:
    offered, retired = ctrl._read_jobs(
        json.dumps(
            [
                job(1),
                job(2, "JobAssigned"),
                job(3, "JobCompleted", runnerName="pool-a"),
                "мусор",
            ]
        )
    )
    assert offered == [1]
    assert retired == ["pool-a"]


def test_a_broken_body_is_not_fatal() -> None:
    assert ctrl._read_jobs("не json") == ([], [])
    assert ctrl._read_jobs("") == ([], [])


def test_scaling_up_submits_one_task_per_assigned_job() -> None:
    ctx = _ctx(pool=FakePool(run=False))
    ctrl._scale(ctx, Stats(assigned=3), "тест")

    assert ctx.fleet.size() == 3
    assert len(ctx.pool.tasks) == 3  # pyright: ignore[reportAttributeAccessIssue]
    payload = next(iter(ctx.pool.tasks.values()))  # pyright: ignore[reportAttributeAccessIssue]
    assert payload.entry == "agent"
    assert payload.kwargs["jit"].startswith("джит-для-")
    assert payload.kwargs["version"] == "2.999.0"
    assert payload.timeout is not None
    assert payload.timeout > ctx.target.lifetime


def test_scaling_never_passes_the_ceiling() -> None:
    ctx = _ctx(Target(slug="owner/app", token="ghp", jobs=2))
    ctrl._scale(ctx, Stats(assigned=9), "тест")
    assert ctx.fleet.size() == 2


def test_a_second_look_does_not_double_the_fleet() -> None:
    ctx = _ctx()
    ctrl._scale(ctx, Stats(assigned=2), "первый")
    ctrl._scale(ctx, Stats(assigned=2), "второй")
    assert ctx.fleet.size() == 2


def test_acquired_jobs_are_counted_before_the_statistics_catch_up() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    ctrl._handle(ctx, session, message(1, [job(7), job(8)], Stats(available=2)))

    assert ctx.fleet.size() == 2
    assert ctx.latest.get().assigned == 2


def test_only_unstarted_runners_are_taken_back() -> None:
    ctx = _ctx()
    ctrl._scale(ctx, Stats(assigned=3), "рост")
    started = ctx.fleet.slots()[0]
    ctx.fleet.mark(started.task_id, "running")

    ctrl._scale(ctx, Stats(assigned=1), "спад", shrink=True)

    assert [slot.task_id for slot in ctx.fleet.slots()] == [started.task_id]
    assert started.task_id not in ctx.pool.cancelled  # pyright: ignore[reportAttributeAccessIssue]


def test_a_running_runner_is_never_cancelled_by_scale_down() -> None:
    ctx = _ctx()
    ctrl._scale(ctx, Stats(assigned=2), "рост")
    for slot in ctx.fleet.slots():
        ctx.fleet.mark(slot.task_id, "running")

    ctrl._scale(ctx, Stats(assigned=0), "спад", shrink=True)

    assert ctx.fleet.size() == 2
    assert ctx.pool.cancelled == []  # pyright: ignore[reportAttributeAccessIssue]


def test_a_message_never_cancels_a_waiting_runner() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")
    ctrl._scale(ctx, Stats(assigned=3), "рост")

    ctrl._handle(ctx, session, message(1, [], Stats(assigned=1)))

    assert ctx.fleet.size() == 3
    assert ctx.pool.cancelled == []  # pyright: ignore[reportAttributeAccessIssue]


def test_a_runner_that_outlived_its_lifetime_is_snapped() -> None:
    pool = FakePool(run=False)
    ctx = _ctx(target=Target(slug="owner/app", token="ghp", lifetime=0.0), pool=pool)
    ctrl._scale(ctx, Stats(assigned=1), "рост")
    slot = ctx.fleet.slots()[0]
    pool.tasks[slot.task_id].status = "running"
    slot.born -= 10_000

    ctrl._reconcile(ctx)

    assert ctx.fleet.size() == 0
    assert pool.cancelled == [slot.task_id]


def test_a_spent_runner_stops_counting_as_capacity() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")
    ctrl._scale(ctx, Stats(assigned=2), "рост")
    worked, waiting = ctx.fleet.slots()
    ctx.fleet.mark(worked.task_id, "running")

    ctrl._handle(
        ctx,
        session,
        message(1, [job(1, "JobCompleted", runnerName=worked.name)], Stats(assigned=1)),
    )
    ctrl._scale(ctx, ctx.latest.get(), "сверка", shrink=True)

    assert ctx.fleet.size() == 1
    assert waiting.task_id not in ctx.pool.cancelled  # pyright: ignore[reportAttributeAccessIssue]


def test_a_finished_task_frees_the_slot() -> None:
    pool = FakePool(run=False)
    ctx = _ctx(pool=pool)
    ctrl._scale(ctx, Stats(assigned=1), "рост")
    task_id = ctx.fleet.slots()[0].task_id
    pool.tasks[task_id].status = "done"

    ctrl._reconcile(ctx)

    assert ctx.fleet.size() == 0


def test_a_lost_task_is_reported_with_its_tail() -> None:
    pool = FakePool(run=False)
    ctx = _ctx(pool=pool)
    ctrl._scale(ctx, Stats(assigned=1), "рост")
    task_id = ctx.fleet.slots()[0].task_id
    pool.tasks[task_id].status = "lost"
    pool.tasks[task_id].error = "worker gone"

    ctrl._reconcile(ctx)

    assert ctx.fleet.size() == 0
    assert "worker gone" in ctrl._why(ctx, task_id, pool.state(task_id))


def test_a_runner_whose_worker_vanished_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(run=False)
    ctx = _ctx(pool=pool)
    ctrl._scale(ctx, Stats(assigned=1), "рост")
    slot = ctx.fleet.slots()[0]
    pool.tasks[slot.task_id].status = "running"

    ctrl._reconcile(ctx)
    assert ctx.fleet.size() == 1

    monkeypatch.setattr(ctrl, "WORKER_STALE", -1.0)
    monkeypatch.setattr(ctrl, "_held", lambda _ctx: {"чужая задача"})
    ctrl._reconcile(ctx)

    assert ctx.fleet.size() == 0
    assert pool.cancelled == [slot.task_id]


def test_a_message_acquires_and_scales() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    ctrl._handle(ctx, session, message(1, [job(7)], Stats(available=1)))

    assert api.acquired == [7]
    assert ctx.fleet.size() == 1
    assert ctx.latest.get().available == 1
    assert ctx.latest.get().assigned == 1


def test_a_repeated_message_is_only_acknowledged() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")
    api.offer(job(7), stats=Stats(available=1))

    stop = threading.Event()
    last = ctrl._tick(ctx, session, 0, stop)
    api.messages.append(message(last, [job(7)], Stats(assigned=1, available=0)))
    again = ctrl._tick(ctx, session, last, stop)

    assert again == last
    assert api.acquired == [7]
    assert api.acked == [1, 1]


def test_cleanup_leaves_busy_runners_alone() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"), Stats(running=1))
    pool = FakePool(run=False)
    ctx = _ctx(
        api=api, pool=pool, target=Target(slug="owner/app", token="ghp", drain=0)
    )
    session = api.open(42, "тест")
    ctrl._scale(ctx, Stats(assigned=2), "рост")
    ctx.fleet.mark(ctx.fleet.slots()[0].task_id, "running")
    ctx.latest.set(Stats(running=1))

    ctrl._cleanup(ctx, session)

    assert api.dropped == []
    assert len(pool.cancelled) == 1


def test_cleanup_takes_everything_down() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    pool = FakePool(run=False)
    ctx = _ctx(api=api, pool=pool)
    session = api.open(42, "тест")
    ctrl._scale(ctx, Stats(assigned=2), "рост")

    ctrl._cleanup(ctx, session)

    assert api.closed == [session.session_id]
    assert api.dropped == [42]
    assert len(pool.cancelled) == 2
    assert ctx.fleet.size() == 0


def test_a_public_repo_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctrl, "preflight", lambda _target: {"private": False})
    monkeypatch.setattr(ctrl, "ScaleSet", FakeScaleSet)
    monkeypatch.setattr(ctrl, "Pool", FakePool)

    ctx = ctrl._start(
        Target(slug="owner/app", token="ghp"),
        Server(url="https://pool", token="t"),
    )
    assert ctx.scale_set_id == 42


def test_a_runner_is_unregistered_when_the_pool_refuses_it() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    pool = FakePool(run=False)
    ctx = _ctx(api=api, pool=pool)

    def refuse(*_args: object, **_kw: object) -> str:
        raise RunnerError("пул лежит")

    pool.submit = refuse  # pyright: ignore[reportAttributeAccessIssue]
    ctrl._scale(ctx, Stats(assigned=4), "рост")

    assert ctx.fleet.size() == 0
    assert api.forgotten == sorted(api.forgotten)
    assert len(api.forgotten) == len(api.jits)
    assert len(api.jits) < 4


def test_the_shipped_code_is_the_agent_module() -> None:
    assert "def agent(" in ctrl.CODE
    assert "ACTIONS_RUNNER_INPUT_JITCONFIG" in ctrl.CODE
    assert "pool.fleet.runners" not in ctrl.CODE


def test_a_restart_picks_up_the_previous_life(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctrl, "preflight", lambda _target: {"private": True})
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    pool = FakePool(run=False)
    monkeypatch.setattr(ctrl, "ScaleSet", lambda _target: api)
    monkeypatch.setattr(ctrl, "Pool", lambda _server: pool)

    target = Target(slug="owner/app", token="ghp", jobs=4)
    first = ctrl._start(target, Server(url="https://pool", token="t"))
    ctrl._scale(first, Stats(assigned=2), "рост")
    for slot in first.fleet.slots():
        pool.tasks[slot.task_id].status = "running"

    second = ctrl._start(target, Server(url="https://pool", token="t"))

    assert second.fleet.size() == 2
    assert {slot.name for slot in second.fleet.slots()} == {
        slot.name for slot in first.fleet.slots()
    }

    ctrl._scale(second, Stats(assigned=2), "после перезапуска")
    assert len(pool.tasks) == 2


def test_someone_elses_runners_are_not_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctrl, "preflight", lambda _target: {"private": True})
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    pool = FakePool(run=False)
    monkeypatch.setattr(ctrl, "ScaleSet", lambda _target: api)
    monkeypatch.setattr(ctrl, "Pool", lambda _server: pool)

    alien = _ctx(target=Target(slug="owner/чужая", token="ghp"), pool=pool, api=api)
    ctrl._scale(alien, Stats(assigned=2), "чужой рост")

    ctx = ctrl._start(
        Target(slug="owner/app", token="ghp"), Server(url="https://pool", token="t")
    )

    assert ctx.fleet.size() == 0


def test_a_failed_run_leaves_the_scale_set_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctrl, "preflight", lambda _target: {"private": True})
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    pool = FakePool(run=False)
    monkeypatch.setattr(ctrl, "ScaleSet", lambda _target: api)
    monkeypatch.setattr(ctrl, "Pool", lambda _server: pool)

    def falls(*_args: object, **_kw: object) -> None:
        raise RunnerError("гитхаб отвалился")

    monkeypatch.setattr(ctrl, "_tick", falls)
    monkeypatch.setattr(ctrl, "backoff", lambda *_a, **_kw: 0.0)

    with pytest.raises(RunnerError):
        ctrl.run(
            Target(slug="owner/app", token="ghp"),
            Server(url="https://pool", token="t"),
            threading.Event(),
        )

    assert api.dropped == []
    assert api.closed
    assert pool.cancelled == []


def test_a_cancelled_runner_loses_its_registration() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    ctrl._scale(ctx, Stats(assigned=1), "рост")
    slot = ctx.fleet.slots()[0]

    ctrl._cancel(ctx, slot.task_id)

    assert ctx.fleet.size() == 0
    assert api.forgotten == [slot.runner_id]


def test_a_job_id_that_is_not_a_number_is_skipped() -> None:
    offered, retired = ctrl._read_jobs(
        json.dumps(
            [
                {"messageType": "JobAvailable", "runnerRequestId": "не число"},
                {"messageType": "JobAvailable", "runnerRequestId": 0},
                job(4),
            ]
        )
    )
    assert offered == [4]
    assert retired == []


def test_junk_statistics_do_not_break_a_message() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    ctrl._handle(
        ctx,
        session,
        {
            "messageId": "пятое",
            "messageType": "RunnerScaleSetJobMessages",
            "body": json.dumps([job(7)]),
            "statistics": {"totalAssignedJobs": "много"},
        },
    )

    assert api.acquired == [7]
    assert ctx.latest.get().assigned == 1


def test_a_message_without_an_id_is_not_acknowledged() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    ctrl._ack(ctx, session, {"messageType": "RunnerScaleSetJobMessages"})

    assert api.acked == []


def test_a_message_of_another_type_is_ignored() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    ctrl._handle(ctx, session, {"messageType": "RunnerScaleSetJobMessagesOther"})

    assert api.acquired == []
    assert ctx.fleet.size() == 0


def test_a_strange_acquirable_list_does_not_stop_the_pick_up() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")
    api.acquirable = lambda _scale_set_id: [  # pyright: ignore[reportAttributeAccessIssue]
        {"нет": "ключа"},
        {"runnerRequestId": "не число"},
        {"runnerRequestId": "9"},
    ]

    ctrl._pick_up(ctx, session, Stats(available=1), "тишина")

    assert api.acquired == [9]


def test_a_session_that_never_opens_reports_the_real_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Target(slug="owner/app", token="ghp")
    api = FakeScaleSet(target)
    ctx = _ctx(target=target, api=api, pool=FakePool(run=False))
    monkeypatch.setattr(ctrl, "_start", lambda *_args: ctx)

    def refuse(*_args: object) -> None:
        raise RunnerError("сессия не открылась")

    monkeypatch.setattr(api, "reopen", refuse)

    with pytest.raises(RunnerError, match="сессия не открылась"):
        ctrl.run(target, Server(url="https://pool", token="t"), threading.Event())


def test_an_expired_queue_token_is_only_refreshed() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    fresh, reset = ctrl._recover(ctx, session, "хозяин", 401)

    assert reset == -1
    assert fresh.session_id != session.session_id


def test_a_dead_session_is_opened_from_scratch() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    fresh, reset = ctrl._recover(ctx, session, "хозяин", 404)

    assert reset == 0
    assert fresh.session_id != session.session_id


def test_a_session_that_cannot_be_restored_is_kept_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    def refuse(*_args: object) -> None:
        raise RunnerError("гитхаб молчит")

    monkeypatch.setattr(api, "refresh", refuse)
    monkeypatch.setattr(api, "reopen", refuse)

    fresh, reset = ctrl._recover(ctx, session, "хозяин", 401)

    assert fresh is session
    assert reset == -1


def test_a_fresh_token_is_left_alone() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")

    assert ctrl._fresh(ctx, session) is session


def test_a_stale_token_is_renewed_before_polling() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"), Stats(assigned=1))
    ctx = _ctx(api=api)
    session = api.open(42, "тест")
    stale = replace(session, queue_token_exp=time.time() - 1)

    fresh = ctrl._fresh(ctx, stale)

    assert fresh.session_id != stale.session_id
    assert ctx.latest.get().assigned == 1


def test_draining_waits_for_nothing_when_told_not_to() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"), Stats(running=3))
    ctx = _ctx(target=Target(slug="owner/app", token="ghp", drain=0), api=api)

    assert ctrl._drain(ctx).running == 0


def test_draining_stops_as_soon_as_the_jobs_are_done() -> None:
    api = FakeScaleSet(Target(slug="owner/app", token="ghp"), Stats(running=0))
    ctx = _ctx(target=Target(slug="owner/app", token="ghp", drain=30), api=api)

    assert ctrl._drain(ctx).running == 0


def test_a_pool_that_says_nothing_leaves_the_fleet_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(run=False)
    ctx = _ctx(pool=pool)
    ctrl._scale(ctx, Stats(assigned=2), "рост")

    def refuse(_task_id: str) -> dict[str, object]:
        raise RunnerError("пул молчит")

    monkeypatch.setattr(pool, "state", refuse)
    ctrl._reconcile(ctx)

    assert ctx.fleet.size() == 2
    assert pool.cancelled == []


def test_a_task_the_pool_forgot_frees_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool(run=False)
    ctx = _ctx(pool=pool)
    ctrl._scale(ctx, Stats(assigned=1), "рост")

    def gone(_task_id: str) -> dict[str, object]:
        raise refused(404)

    monkeypatch.setattr(pool, "state", gone)
    ctrl._reconcile(ctx)

    assert ctx.fleet.size() == 0
