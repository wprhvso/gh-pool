from types import TracebackType
from typing import Any, ClassVar, Self
from uuid import uuid4

import httpx
import pytest

from gh_pool.core.config import settings
from gh_pool.server import pool

SESSION = uuid4()
RUNNER_TOKEN = "the-token-the-runner-comes-back-with"


class FakeClient:
    sent: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, answer: httpx.Response | Exception, **_kwargs: Any) -> None:
        self._answer = answer

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return

    async def post(
        self, url: str, json: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        FakeClient.sent.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "pool_server", "https://pool.example.com/")
    monkeypatch.setattr(settings, "pool_token", "a-pool-secret")
    monkeypatch.setattr(settings, "public_url", "https://chrome.example.com")
    monkeypatch.setattr(settings, "runner_spec", "gh-pool[browser]==9.9.9")
    FakeClient.sent = []
    return monkeypatch


def _answers(monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception):
    monkeypatch.setattr(
        pool.httpx, "AsyncClient", lambda **kwargs: FakeClient(answer, **kwargs)
    )


async def test_a_task_is_submitted_with_everything_the_runner_needs(
    configured: pytest.MonkeyPatch,
):
    _answers(configured, httpx.Response(201, json={"task_id": "t-1"}))

    await pool.dispatch(SESSION, RUNNER_TOKEN)

    sent = FakeClient.sent[-1]
    assert sent["url"] == "https://pool.example.com/v1/tasks"
    assert sent["headers"]["Authorization"] == "Bearer a-pool-secret"
    kwargs = sent["json"]["payload"]["kwargs"]
    assert kwargs["session_id"] == str(SESSION)
    assert kwargs["token"] == RUNNER_TOKEN
    assert kwargs["url"] == "https://chrome.example.com"
    assert kwargs["spec"] == "gh-pool[browser]==9.9.9"
    assert "gh-pool-browser" in sent["json"]["payload"]["code"]


async def test_a_pool_that_was_never_configured_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "pool_server", "")
    monkeypatch.setattr(settings, "pool_token", "")
    FakeClient.sent = []

    with pytest.raises(pool.DispatchError, match="not configured"):
        await pool.dispatch(SESSION, RUNNER_TOKEN)

    assert FakeClient.sent == []


async def test_a_pool_that_cannot_be_reached_is_a_dispatch_failure(
    configured: pytest.MonkeyPatch,
):
    _answers(configured, httpx.ConnectError("connection refused"))

    with pytest.raises(pool.DispatchError, match="unreachable"):
        await pool.dispatch(SESSION, RUNNER_TOKEN)


async def test_a_pool_that_takes_too_long_is_a_dispatch_failure(
    configured: pytest.MonkeyPatch,
):
    _answers(configured, httpx.ReadTimeout("took too long"))

    with pytest.raises(pool.DispatchError):
        await pool.dispatch(SESSION, RUNNER_TOKEN)


@pytest.mark.parametrize("code", [400, 401, 429, 500, 503])
async def test_a_pool_that_says_no_is_a_dispatch_failure(
    configured: pytest.MonkeyPatch, code: int
):
    _answers(configured, httpx.Response(code, text="not today"))

    with pytest.raises(pool.DispatchError, match=str(code)):
        await pool.dispatch(SESSION, RUNNER_TOKEN)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>a proxy said hello</html>"),
        httpx.Response(200, content=b""),
    ],
)
async def test_an_answer_that_is_not_json_is_a_dispatch_failure(
    configured: pytest.MonkeyPatch, answer: httpx.Response
):
    _answers(configured, answer)

    with pytest.raises(pool.DispatchError, match="json"):
        await pool.dispatch(SESSION, RUNNER_TOKEN)


@pytest.mark.parametrize("payload", [{"nothing": "useful"}, {}, [1, 2], "a string"])
async def test_an_answer_without_a_task_id_is_a_dispatch_failure(
    configured: pytest.MonkeyPatch, payload: object
):
    _answers(configured, httpx.Response(200, json=payload))

    with pytest.raises(pool.DispatchError, match="task_id"):
        await pool.dispatch(SESSION, RUNNER_TOKEN)


def test_the_runner_spec_is_the_one_the_operator_pinned(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "runner_spec", "gh-pool[browser] @ git+ssh://x")

    assert pool.runner_spec() == "gh-pool[browser] @ git+ssh://x"


def test_without_a_pin_the_runner_matches_the_server_that_dispatched_it(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "runner_spec", "")
    monkeypatch.setattr(pool, "version", lambda _name: "1.2.3")

    assert pool.runner_spec() == "gh-pool[browser]==1.2.3"
