from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from pool.keeper import api as api_mod
from pool.keeper.api import ScaleSet
from pool.keeper.config import Target
from pool.keeper.errors import HttpError, RunnerError
from pool.keeper.models import Session
from tests.keeper.fake import refused

PIPELINE = "https://x.pipelines.actions.githubusercontent.com/OPAQUE"
QUEUE = "https://q.example/queue?sessionId=abc"


def _jwt(exp: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"head.{claims.rstrip('=')}.sig"


def _raw_session(**extra: Any) -> dict[str, Any]:
    return {
        "sessionId": "s-1",
        "messageQueueUrl": QUEUE,
        "messageQueueAccessToken": _jwt(time.time() + 3600),
        **extra,
    }


class Wire:
    def __init__(self, *answers: object) -> None:
        self.answers: list[object] = list(answers) or [{}]
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.auths: int = 0
        self.exp: float = time.time() + 3600.0
        self.registration: object = None

    def __call__(self, method: str, url: str, **kw: Any) -> Any:
        if url.endswith("/actions/runner-registration"):
            self.auths += 1
            if self.registration is not None:
                return self.registration
            return {"token": _jwt(self.exp), "url": PIPELINE + "/"}
        self.calls.append((method, url, kw))
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def urls(self) -> list[str]:
        return [url for _method, url, _kw in self.calls]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    def install(*answers: object) -> Wire:
        made = Wire(*answers)
        monkeypatch.setattr(api_mod, "request", made)
        monkeypatch.setattr(api_mod, "registration_token", lambda _target: "рег")
        monkeypatch.setattr(api_mod, "pause", lambda _seconds: False)
        return made

    return install


@pytest.fixture
def target() -> Target:
    return Target(slug="owner/app", token="ghp", label="pool")


def test_the_pipeline_token_is_taken_once(wire, target: Target) -> None:
    made = wire({"value": []})
    api = ScaleSet(target)

    api.find("pool")
    api.find("pool")

    assert made.auths == 1
    assert all(url.startswith(PIPELINE + "/") for url in made.urls())
    assert all("api-version=" in url for url in made.urls())


def test_a_short_lived_token_is_taken_again(wire, target: Target) -> None:
    made = wire({"value": []})
    made.exp = time.time() + 10
    api = ScaleSet(target)

    api.find("pool")
    api.find("pool")

    assert made.auths > 1


def test_a_registration_without_a_token_is_refused(wire, target: Target) -> None:
    made = wire()
    made.registration = {"url": PIPELINE}

    with pytest.raises(RunnerError):
        _ = ScaleSet(target).pipeline_url


def test_a_rejected_token_is_taken_again_and_the_call_repeats(
    wire, target: Target
) -> None:
    made = wire(refused(401), {"id": 42})
    api = ScaleSet(target)

    assert api.statistics(42) is not None
    assert made.auths == 2
    assert len(made.calls) == 2


def test_a_rejected_token_is_not_chased_forever(wire, target: Target) -> None:
    made = wire(refused(401))
    with pytest.raises(HttpError):
        ScaleSet(target).statistics(42)
    assert len(made.calls) == 2


def test_a_scale_set_is_found_by_exact_name(wire, target: Target) -> None:
    made = wire({"value": [{"name": "pool-other", "id": 1}, {"name": "pool", "id": 2}]})
    found = ScaleSet(target).find("pool")

    assert found is not None
    assert found["id"] == 2
    assert "name=pool" in made.urls()[0]


def test_a_missing_scale_set_is_not_an_error(wire, target: Target) -> None:
    wire(refused(404))
    assert ScaleSet(target).find("pool") is None


def test_another_failure_while_searching_is_raised(wire, target: Target) -> None:
    wire(refused(500))
    with pytest.raises(HttpError):
        ScaleSet(target).find("pool")


def test_an_existing_scale_set_is_reused(wire, target: Target) -> None:
    made = wire({"value": [{"name": "pool", "id": 7}]})
    scale_set, created = ScaleSet(target).ensure("pool")

    assert (scale_set["id"], created) == (7, False)
    assert len(made.calls) == 1


def test_a_missing_scale_set_is_created_ephemeral(wire, target: Target) -> None:
    made = wire({"value": []}, {"id": 9, "name": "pool"})
    scale_set, created = ScaleSet(target).ensure("pool")

    assert (scale_set["id"], created) == (9, True)
    body = made.calls[1][2]["body"]
    assert body["runnerSetting"] == {"ephemeral": True, "disableUpdate": True}
    assert body["labels"] == [{"name": "pool", "type": "System"}]


def test_a_scale_set_that_was_not_created_is_reported(wire, target: Target) -> None:
    wire({"value": []}, {"нет": "id"})
    with pytest.raises(RunnerError):
        ScaleSet(target).ensure("pool")


def test_dropping_an_already_gone_scale_set_is_quiet(wire, target: Target) -> None:
    wire(refused(404))
    ScaleSet(target).drop(42)


def test_dropping_raises_on_anything_else(wire, target: Target) -> None:
    wire(refused(500))
    with pytest.raises(HttpError):
        ScaleSet(target).drop(42)


def test_a_jit_config_carries_the_work_folder(wire, target: Target) -> None:
    made = wire({"encodedJITConfig": "джит", "runner": {"id": 11}})
    runner_id, name, config = ScaleSet(target).jit(42)

    assert (runner_id, config) == (11, "джит")
    assert name.startswith("pool-")
    assert made.calls[0][2]["body"]["workFolder"] == target.work
    assert made.calls[0][2]["body"]["name"] == name


def test_an_empty_jit_config_is_refused(wire, target: Target) -> None:
    wire({"encodedJITConfig": ""})
    with pytest.raises(RunnerError):
        ScaleSet(target).jit(42)


def test_a_jit_without_a_runner_id_still_works(wire, target: Target) -> None:
    wire({"encodedJITConfig": "джит"})
    runner_id, _name, _config = ScaleSet(target).jit(42)
    assert runner_id == 0


def test_forgetting_a_runner_hits_the_agents_endpoint(wire, target: Target) -> None:
    made = wire({})
    assert ScaleSet(target).forget(5) is True
    assert "distributedtask/pools/0/agents/5" in made.urls()[0]


def test_forgetting_nobody_touches_nothing(wire, target: Target) -> None:
    made = wire({})
    assert ScaleSet(target).forget(0) is False
    assert made.calls == []


def test_a_registration_that_will_not_go_is_not_fatal(wire, target: Target) -> None:
    wire(refused(500))
    assert ScaleSet(target).forget(5) is False


def test_statistics_come_back_parsed(wire, target: Target) -> None:
    wire({"statistics": {"totalAssignedJobs": 3}})
    assert ScaleSet(target).statistics(42).assigned == 3


def test_a_session_is_opened_with_an_owner(wire, target: Target) -> None:
    made = wire(_raw_session(statistics={"totalIdleRunners": 2}))
    session = ScaleSet(target).open(42, "хозяин")

    assert session.session_id == "s-1"
    assert session.queue_url == QUEUE
    assert session.stats.idle == 2
    assert session.queue_token_exp > time.time()
    assert made.calls[0][2]["body"] == {"ownerName": "хозяин"}


def test_a_session_without_a_queue_is_refused(wire, target: Target) -> None:
    wire({"sessionId": "s-1"})
    with pytest.raises(RunnerError):
        ScaleSet(target).open(42, "хозяин")


def test_a_refreshed_session_replaces_the_token(wire, target: Target) -> None:
    made = wire(_raw_session(messageQueueAccessToken=_jwt(time.time() + 7200)))
    old = Session("s-0", QUEUE, "старый", 0.0)
    fresh = ScaleSet(target).refresh(42, old)

    assert fresh.queue_token != old.queue_token
    assert made.calls[0][0] == "PATCH"
    assert "sessions/s-0" in made.urls()[0]


def test_closing_a_gone_session_is_quiet(wire, target: Target) -> None:
    wire(refused(404))
    ScaleSet(target).close(42, Session("s-0", QUEUE, "t", 0.0))


def test_a_conflicting_session_is_waited_out(wire, target: Target) -> None:
    made = wire(refused(409), refused(409), _raw_session())
    session = ScaleSet(target).reopen(42, None, "хозяин")

    assert session.session_id == "s-1"
    assert len(made.calls) == 3


def test_a_session_that_never_frees_up_gives_up(
    wire, target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_mod, "SESSION_CONFLICT_TRIES", 3)
    made = wire(refused(409))

    with pytest.raises(HttpError):
        ScaleSet(target).reopen(42, None, "хозяин")

    assert len(made.calls) == 3


def test_reopening_closes_the_old_session_first(wire, target: Target) -> None:
    made = wire(_raw_session())
    ScaleSet(target).reopen(42, Session("s-0", QUEUE, "t", 0.0), "хозяин")

    assert made.calls[0][0] == "DELETE"
    assert made.calls[1][0] == "POST"


def test_a_stubborn_old_session_does_not_block_the_new_one(
    wire, target: Target
) -> None:
    made = wire(refused(500), _raw_session())
    session = ScaleSet(target).reopen(42, Session("s-0", QUEUE, "t", 0.0), "хозяин")
    assert session.session_id == "s-1"
    assert len(made.calls) == 2


def test_polling_asks_from_the_last_message(wire, target: Target) -> None:
    made = wire({"messageId": 5})
    session = Session("s-1", QUEUE, "очередь", 0.0)

    assert ScaleSet(target).poll(session, 4, capacity=7) == {"messageId": 5}
    url, kw = made.urls()[0], made.calls[0][2]
    assert "lastMessageId=4" in url
    assert kw["extra"][api_mod.CAPACITY_HEADER] == "7"
    assert kw["auth"] == "Bearer очередь"
    assert kw["attempts"] == 1


def test_polling_the_first_time_asks_for_everything(wire, target: Target) -> None:
    made = wire(None)
    assert ScaleSet(target).poll(Session("s-1", QUEUE, "t", 0.0)) is None
    assert "lastMessageId" not in made.urls()[0]


def test_a_message_is_acknowledged_on_the_queue(wire, target: Target) -> None:
    made = wire({})
    ScaleSet(target).ack(Session("s-1", QUEUE, "t", 0.0), 5)
    assert made.urls()[0] == "https://q.example/queue/5?sessionId=abc"


def test_a_queue_without_a_query_is_acknowledged_cleanly(wire, target: Target) -> None:
    made = wire({})
    ScaleSet(target).ack(Session("s-1", "https://q.example/queue", "t", 0.0), 5)
    assert made.urls()[0] == "https://q.example/queue/5"


def test_acquirable_jobs_come_as_a_list(wire, target: Target) -> None:
    wire({"value": [{"runnerRequestId": 1}]})
    assert ScaleSet(target).acquirable(42) == [{"runnerRequestId": 1}]


def test_junk_among_acquirable_jobs_is_dropped(wire, target: Target) -> None:
    wire({"value": ["мусор", {"runnerRequestId": 1}]})
    assert ScaleSet(target).acquirable(42) == [{"runnerRequestId": 1}]


def test_no_acquirable_jobs_is_an_empty_list(wire, target: Target) -> None:
    wire(None)
    assert ScaleSet(target).acquirable(42) == []


def test_jobs_are_acquired_with_the_queue_token(wire, target: Target) -> None:
    made = wire({"value": [1, 2]})
    session = Session("s-1", QUEUE, "очередь", 0.0)

    assert ScaleSet(target).acquire(42, session, [1, 2, 3]) == 2
    assert made.calls[0][2]["auth"] == "Bearer очередь"
    assert made.calls[0][2]["body"] == [1, 2, 3]


def test_acquiring_nothing_touches_nothing(wire, target: Target) -> None:
    made = wire({})
    assert ScaleSet(target).acquire(42, Session("s", QUEUE, "t", 0.0), []) == 0
    assert made.calls == []
