from typing import Any

import pytest

from pool.protocol import (
    CommandError,
    ErrorCode,
    Method,
    SessionParams,
    Topic,
)
from pool.browser.actions import Actions
from pool.browser.cdp import CdpError
from pool.browser.locate import ElementIntercepted, ElementMissing
from pool.browser.navigation import NavigationFailed
from pool.browser.tabs import NoActiveTab


class FakeCdp:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def off(self, event: str, _handler: Any = None) -> None:
        self.listeners.pop(event, None)

    async def send(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}


class FakeXtest:
    def close(self) -> None:
        return


class FakeServer:
    async def event(self, _data: Any) -> None:
        return


@pytest.fixture
def actions() -> Actions:
    return Actions(FakeCdp(), FakeXtest(), FakeServer(), SessionParams())  # pyright: ignore[reportArgumentType]


def test_every_command_the_protocol_names_has_something_to_run_it(actions: Actions):
    assert set(actions._handlers) == set(Method)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ElementMissing("#nope"), ErrorCode.NOT_FOUND),
        (ElementIntercepted("#covered"), ErrorCode.INTERCEPTED),
        (NavigationFailed("net::ERR"), ErrorCode.NAVIGATION_FAILED),
        (TimeoutError("took too long"), ErrorCode.TIMEOUT),
        (CdpError("Page.navigate", "no"), ErrorCode.RUNNER_ERROR),
    ],
)
def test_a_failure_is_reported_under_the_code_that_names_it(
    actions: Actions, failure: Exception, code: ErrorCode
):
    reported = actions.to_error(failure)

    assert isinstance(reported, CommandError)
    assert reported.code is code


def test_a_failure_nobody_planned_for_is_still_reported(actions: Actions):
    reported = actions.to_error(NoActiveTab("session has no active tab"))

    assert reported.code is ErrorCode.RUNNER_ERROR
    assert "NoActiveTab" in reported.message


def test_a_failure_with_nothing_to_say_still_says_something(actions: Actions):
    assert actions.to_error(ElementMissing()).message


async def test_a_session_that_asked_for_tabs_is_told_about_them(actions: Actions):
    await actions.subscribe([Topic.TABS])

    assert actions.tabs.on_event is not None


async def test_a_session_that_asked_for_downloads_is_watching_for_them(
    actions: Actions,
):
    await actions.subscribe([Topic.DOWNLOADS])

    assert actions.files._watching


async def test_a_session_that_asked_for_nothing_hears_nothing(actions: Actions):
    await actions.subscribe([Topic.TABS, Topic.DOWNLOADS])

    await actions.subscribe([])

    assert actions.tabs.on_event is None
    assert not actions.files._watching
