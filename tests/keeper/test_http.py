from __future__ import annotations

import urllib.request
from typing import Any

import pytest
from pool_runners import http as http_mod
from pool_runners.budget import Budget
from pool_runners.errors import HttpError, RateLimited, RunnerError
from tests.conftest import Clock, Response, headers, http_error


class Calls:
    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.seen: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request, **_kw: Any) -> object:
        self.seen.append(req)
        answer = self.answers[min(len(self.seen) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch):
    def install(*answers: object) -> Calls:
        made = Calls(*answers)
        monkeypatch.setattr(urllib.request, "urlopen", made)
        monkeypatch.setattr(http_mod, "backoff", lambda *_, **__: 0.0)
        return made

    return install


def test_json_round_trip(calls) -> None:
    made = calls(Response(body=b'{"ok": true}'))
    assert http_mod.request("GET", "https://api.github.com/x") == {"ok": True}
    assert made.seen[0].get_header("User-agent", "").startswith("pool-runners/")


def test_empty_answer_is_none(calls) -> None:
    calls(Response(status=202, body=b""))
    assert http_mod.request("GET", "https://pipelines.example/queue") is None


def test_closed_gate_refuses_without_touching_the_network(calls, clock: Clock) -> None:
    budget = Budget()
    budget.refuse(403, headers(Retry_After="60"), "rate limit")
    made = calls(Response())

    with pytest.raises(RateLimited) as caught:
        http_mod.fetch("GET", "https://api.github.com/x", budget=budget)

    assert caught.value.retry_in == 60
    assert made.seen == []


def test_rate_limited_answer_becomes_rate_limited_error(calls, clock: Clock) -> None:
    budget = Budget()
    made = calls(
        http_error(
            403,
            b'{"message": "API rate limit exceeded for user ID 1."}',
            X_RateLimit_Remaining="0",
            X_RateLimit_Reset=str(clock.wall + 90),
        )
    )

    with pytest.raises(RateLimited) as caught:
        http_mod.fetch("GET", "https://api.github.com/x", budget=budget, attempts=5)

    assert caught.value.retry_in == 90
    assert len(made.seen) == 1
    assert budget.shut() == 90


def test_permission_403_still_fails_fast(calls, clock: Clock) -> None:
    budget = Budget()
    made = calls(
        http_error(
            403,
            b'{"message": "Resource not accessible by personal access token"}',
            X_RateLimit_Remaining="4999",
        )
    )

    with pytest.raises(HttpError) as caught:
        http_mod.fetch("GET", "https://api.github.com/x", budget=budget, attempts=5)

    assert not isinstance(caught.value, RateLimited)
    assert len(made.seen) == 1


def test_retries_survive_a_flaky_answer(calls) -> None:
    made = calls(http_error(502), Response(body=b'{"ok": true}'))
    assert http_mod.request("GET", "https://api.github.com/x", attempts=3) == {
        "ok": True
    }
    assert len(made.seen) == 2


def test_stop_cuts_the_retry_ladder_short(calls) -> None:
    made = calls(http_error(502))
    http_mod.STOP.set()

    with pytest.raises(RunnerError):
        http_mod.fetch("GET", "https://api.github.com/x", attempts=5)

    assert len(made.seen) == 2


def test_pipeline_429_is_retried_not_held(calls, clock: Clock) -> None:
    made = calls(http_error(429, b"", Retry_After="0"), Response(body=b"{}"))
    assert http_mod.request("GET", "https://pipelines.example/x", attempts=3) == {}
    assert len(made.seen) == 2


def test_secrets_do_not_reach_the_error_text(calls) -> None:
    made = calls(
        http_error(
            404, b"no", url="https://x.pipelines.actions.githubusercontent.com/SECRET/x"
        )
    )
    with pytest.raises(HttpError) as caught:
        http_mod.fetch(
            "GET",
            "https://x.pipelines.actions.githubusercontent.com/SECRET/x?sessionId=abc-1",
        )
    assert "SECRET" not in str(caught.value)
    assert "<session>" in str(caught.value)
    assert len(made.seen) == 1


def test_jwt_expiry_reads_exp() -> None:
    token = "eyJ0eXAiOiJKV1QifQ.eyJleHAiOjE3MDAwMDAwMDB9.sig"
    assert http_mod.jwt_expiry(token) == 1700000000.0
    assert http_mod.jwt_expiry("мусор") > 0
