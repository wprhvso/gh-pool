from __future__ import annotations

import json
import urllib.parse
from typing import Any

import pytest
from pool_runners import pool as pool_mod
from pool_runners.config import Server
from pool_runners.errors import RunnerError
from pool_runners.http import Reply
from pool_runners.pool import Pool
from tests.conftest import headers


def _reply(body: object = None, **head: str) -> Reply:
    data = b"" if body is None else json.dumps(body).encode()
    return Reply(200, data, headers(**head))


def _row(
    task_id: str, name: str, slug: str = "owner/app", **kwargs: Any
) -> dict[str, Any]:
    return {
        "id": task_id,
        "payload": {"kwargs": {"name": name, "slug": slug, **kwargs}},
    }


class Wire:
    def __init__(self, *answers: object) -> None:
        self.answers: list[object] = list(answers) or [_reply({})]
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> Any:
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
        monkeypatch.setattr(pool_mod, "fetch", made)
        return made

    return install


@pytest.fixture
def pool() -> Pool:
    return Pool(Server(url="https://pool.example", token="секрет"))


def test_every_call_is_signed_with_the_pool_token(wire, pool: Pool) -> None:
    made = wire(_reply({"ok": True}))
    assert pool.health() == {"ok": True}
    assert made.calls[0][2]["auth"] == "Bearer секрет"
    assert made.urls()[0] == "https://pool.example/healthz"


def test_a_runner_is_submitted_with_its_arguments(wire, pool: Pool) -> None:
    made = wire(_reply({"task_id": "t-1"}))
    assert pool.submit("код", "agent", {"name": "pool-a"}, timeout=90.0) == "t-1"

    body = made.calls[0][2]["body"]
    assert body["type"] == "python"
    assert body["payload"]["entry"] == "agent"
    assert body["payload"]["kwargs"] == {"name": "pool-a"}
    assert body["payload"]["timeout"] == 90.0
    assert isinstance(body["payload"]["trace"], dict)


def test_a_submit_without_a_task_id_is_refused(wire, pool: Pool) -> None:
    wire(_reply({}))
    with pytest.raises(RunnerError):
        pool.submit("код", "agent", {})


def test_a_submit_without_a_timeout_does_not_send_one(wire, pool: Pool) -> None:
    made = wire(_reply({"task_id": "t-1"}))
    pool.submit("код", "agent", {})
    assert "timeout" not in made.calls[0][2]["body"]["payload"]


def test_a_task_state_must_be_a_table(wire, pool: Pool) -> None:
    wire(_reply(["не таблица"]))
    with pytest.raises(RunnerError):
        pool.state("t-1")


def test_a_task_state_comes_back_as_given(wire, pool: Pool) -> None:
    made = wire(_reply({"status": "running"}))
    assert pool.state("t-1")["status"] == "running"
    assert made.urls()[0].endswith("/v1/tasks/t-1")


def test_a_cancel_without_an_answer_is_still_fine(wire, pool: Pool) -> None:
    made = wire(_reply(None))
    assert pool.cancel("t-1") == {}
    assert made.urls()[0].endswith("/v1/tasks/t-1/cancel")


def test_the_tail_asks_for_the_size_and_then_the_end(wire, pool: Pool) -> None:
    made = wire(
        Reply(200, b"", headers(X_Event_Size="5000")),
        Reply(200, "хвост".encode(), headers()),
    )
    assert pool.tail("t-1", limit=1000) == "хвост"

    offsets = [
        urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0]
        for url in made.urls()
    ]
    assert offsets == [str(1 << 40), "4000"]


def test_a_short_log_is_taken_from_the_start(wire, pool: Pool) -> None:
    made = wire(Reply(200, b"", headers()), Reply(200, "мало".encode(), headers()))
    assert pool.tail("t-1") == "мало"
    assert made.urls()[1].endswith("offset=0")


def test_own_runners_are_recognised_by_slug_and_label(wire, pool: Pool) -> None:
    wire(
        _reply([_row("t-1", "pool-a"), _row("t-2", "pool-b", slug="other/app")]),
        _reply([_row("t-3", "pool-c", label="иной")]),
    )
    assert pool.mine("owner/app", "pool") == [("t-1", "pool-a", "pending")]


def test_a_runner_without_a_label_is_taken_as_ours(wire, pool: Pool) -> None:
    wire(_reply([_row("t-1", "pool-a")]), _reply([]))
    assert pool.mine("owner/app", "любая") == [("t-1", "pool-a", "pending")]


def test_a_malformed_task_row_is_skipped(wire, pool: Pool) -> None:
    wire(
        _reply(
            [
                "мусор",
                {"id": "t-0"},
                {"id": "", "payload": {"kwargs": {"slug": "owner/app"}}},
                _row("t-1", "pool-a"),
            ]
        ),
        _reply({}),
    )
    assert pool.mine("owner/app", "pool") == [("t-1", "pool-a", "pending")]


def test_both_alive_statuses_are_asked_for(wire, pool: Pool) -> None:
    made = wire(_reply([]))
    pool.mine("owner/app", "pool")
    assert [url.split("status=")[1].split("&")[0] for url in made.urls()] == [
        "pending",
        "running",
    ]


def test_workers_and_health_survive_a_strange_answer(wire, pool: Pool) -> None:
    wire(_reply({"не": "список"}))
    assert pool.workers() == []

    wire(_reply(["не таблица"]))
    assert pool.health() == {}


def test_a_trailing_slash_does_not_double_up_in_paths(wire) -> None:
    made = wire(_reply({}))
    Pool(Server(url="https://pool.example/", token="t")).health()
    assert made.urls()[0] == "https://pool.example/healthz"
