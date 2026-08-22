from __future__ import annotations

from typing import Any

import pytest
from tests.fleet.conftest import headers
from tests.fleet.fake import refused

from gh_pool.fleet.runners import gh as gh_mod
from gh_pool.fleet.runners.config import Target
from gh_pool.fleet.runners.errors import RateLimited, RunnerError
from gh_pool.fleet.runners.http import Reply


class Wire:
    def __init__(self, *answers: object) -> None:
        self.answers: list[object] = list(answers) or [{}]
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> Any:
        self.calls.append((method, url, kw))
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def urls(self) -> list[str]:
        return [url for _method, url, _kw in self.calls]


@pytest.fixture(autouse=True)
def forget_what_was_checked() -> None:
    gh_mod._checked.clear()
    gh_mod._release.clear()


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    def install(*answers: object) -> Wire:
        made = Wire(*answers)
        monkeypatch.setattr(gh_mod, "request", made)
        return made

    return install


@pytest.fixture
def target() -> Target:
    return Target(slug="owner/app", token="ghp")


def test_calls_go_to_the_rest_api_with_a_version(wire, target: Target) -> None:
    made = wire({"private": True})
    assert gh_mod.repo(target)["private"] is True

    _method, url, kw = made.calls[0]
    assert url == "https://api.github.com/repos/owner/app"
    assert kw["auth"] == "Bearer ghp"
    assert kw["extra"]["X-GitHub-Api-Version"] == gh_mod.REST_VERSION
    assert kw["budget"] is gh_mod.REST


def test_a_custom_api_base_is_honoured(
    wire, target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_API_URL", "https://github.example/api/v3/")
    made = wire({"private": True})
    gh_mod.repo(target)
    assert made.urls()[0] == "https://github.example/api/v3/repos/owner/app"


def test_an_invisible_repo_says_what_to_fix(wire, target: Target) -> None:
    wire(refused(404))
    with pytest.raises(RunnerError, match="не виден"):
        gh_mod.repo(target)


def test_a_broken_answer_about_a_repo_is_refused(wire, target: Target) -> None:
    wire(["не таблица"])
    with pytest.raises(RunnerError):
        gh_mod.repo(target)


def test_a_rate_limit_is_not_dressed_up_as_something_else(wire, target: Target) -> None:
    wire(RateLimited(403, "https://api.github.com/x", "перебор", 30.0))
    with pytest.raises(RateLimited):
        gh_mod.repo(target)


def test_a_registration_token_is_returned(wire, target: Target) -> None:
    made = wire({"token": "ARRR"})
    assert gh_mod.registration_token(target) == "ARRR"
    assert made.urls()[0].endswith("/actions/runners/registration-token")


def test_an_empty_registration_token_is_refused(wire, target: Target) -> None:
    wire({"token": ""})
    with pytest.raises(RunnerError):
        gh_mod.registration_token(target)


def test_only_our_runners_are_listed(wire, target: Target) -> None:
    wire(
        {
            "runners": [
                {"id": 1, "name": "pool-a"},
                {"id": 2, "name": "чужой"},
                "мусор",
            ]
        }
    )
    assert [item["id"] for item in gh_mod.runners(target)] == [1]


def test_a_strange_runners_answer_is_an_empty_list(wire, target: Target) -> None:
    wire(None)
    assert gh_mod.runners(target) == []


def test_a_runner_is_deleted(wire, target: Target) -> None:
    made = wire(None)
    assert gh_mod.delete_runner(target, 5) is True
    assert made.calls[0][0] == "DELETE"
    assert made.urls()[0].endswith("/actions/runners/5")


def test_a_runner_that_will_not_delete_is_not_fatal(wire, target: Target) -> None:
    wire(refused(500))
    assert gh_mod.delete_runner(target, 5) is False


def test_the_release_version_is_read_from_the_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def landing(_method: str, url: str, **_kw: Any) -> Reply:
        seen.append(url)
        return Reply(
            200,
            b"",
            headers(),
            "https://github.com/actions/runner/releases/tag/v2.320.0",
        )

    monkeypatch.setattr(gh_mod, "fetch", landing)

    assert gh_mod.release_version() == "2.320.0"
    assert gh_mod.release_version() == "2.320.0"
    assert len(seen) == 1


def test_a_redirect_that_leads_nowhere_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gh_mod,
        "fetch",
        lambda *_a, **_kw: Reply(200, b"", headers(), "https://github.com/actions"),
    )
    with pytest.raises(RunnerError):
        gh_mod.release_version()


def test_the_admin_check_runs_once_per_repo(wire, target: Target) -> None:
    made = wire({"private": True})

    assert gh_mod.preflight(target)["private"] is True
    gh_mod.preflight(target)

    assert [url.rsplit("/", 1)[-1] for url in made.urls()] == [
        "app",
        "runners?per_page=1",
        "app",
    ]


def test_a_token_without_admin_rights_says_so(wire, target: Target) -> None:
    wire({"private": True}, refused(403))
    with pytest.raises(RunnerError, match="прав администратора"):
        gh_mod.preflight(target)


def test_another_failure_in_preflight_is_raised_as_is(wire, target: Target) -> None:
    wire({"private": True}, refused(500))
    with pytest.raises(RunnerError):
        gh_mod.preflight(target)
