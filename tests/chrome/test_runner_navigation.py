import asyncio
from typing import Any

import pytest

from gh_pool.browser import navigation
from gh_pool.browser.cdp import CdpError
from gh_pool.browser.navigation import NavigationFailed
from gh_pool.protocol import Goto, WaitUntil


class FakeTabs:
    def __init__(
        self,
        *,
        documents: list[tuple[str, str]] | None = None,
        ready_states: list[str] | None = None,
        entries: list[str] | None = None,
        current: int = 0,
        inflight: list[int] | None = None,
        navigate_error: str | None = None,
    ) -> None:
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.navigated: list[str] = []
        self._documents = list(documents or [("loader-1", "https://example.com/")])
        self._ready = list(ready_states or ["complete"])
        self._entries = list(entries or ["a", "b", "c"])
        self._current = current
        self._inflight = list(inflight or [0])
        self._navigate_error = navigate_error

    async def navigate(self, url: str) -> None:
        if self._navigate_error is not None:
            raise CdpError("Page.navigate", self._navigate_error)
        self.navigated.append(url)

    async def evaluate(self, expression: str, _tab: object = None) -> Any:
        if expression == "document.readyState":
            return self._ready.pop(0) if len(self._ready) > 1 else self._ready[0]
        raise AssertionError(expression)

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.sent.append((method, params))
        if method == "Page.getFrameTree":
            loader, url = (
                self._documents.pop(0)
                if len(self._documents) > 1
                else self._documents[0]
            )
            return {"frameTree": {"frame": {"loaderId": loader, "url": url}}}
        if method == "Page.getNavigationHistory":
            return {
                "currentIndex": self._current,
                "entries": [{"id": name} for name in self._entries],
            }
        return {}

    def inflight(self, _tab: object = None) -> int:
        return self._inflight.pop(0) if len(self._inflight) > 1 else self._inflight[0]

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.sent]


async def test_a_page_is_asked_for_and_waited_for():
    tabs = FakeTabs()

    await navigation.goto(tabs, Goto(url="https://example.com/form"))

    assert tabs.navigated == ["https://example.com/form"]


async def test_a_page_the_browser_refused_fails_the_command():
    tabs = FakeTabs(navigate_error="net::ERR_CONNECTION_REFUSED")

    with pytest.raises(NavigationFailed, match="ERR_CONNECTION_REFUSED"):
        await navigation.goto(tabs, Goto(url="http://127.0.0.1:1/"))


@pytest.mark.parametrize(
    ("wait_until", "states"),
    [
        (WaitUntil.DOMCONTENTLOADED, ["loading", "interactive"]),
        (WaitUntil.LOAD, ["loading", "interactive", "complete"]),
    ],
)
async def test_a_wait_ends_when_the_document_reached_the_state_it_was_told_to(
    wait_until: WaitUntil, states: list[str]
):
    tabs = FakeTabs(ready_states=states)

    async with asyncio.timeout(5):
        await navigation.settle(tabs, wait_until)


async def test_a_wait_for_the_document_does_not_wait_for_the_network():
    tabs = FakeTabs(ready_states=["complete"], inflight=[3])

    async with asyncio.timeout(5):
        await navigation.settle(tabs, WaitUntil.LOAD)


async def test_a_wait_for_a_quiet_network_outlasts_the_last_request():
    tabs = FakeTabs(ready_states=["complete"], inflight=[2, 1, 0])

    async with asyncio.timeout(10):
        await navigation.settle(tabs, WaitUntil.NETWORKIDLE)

    assert tabs.inflight() == 0


async def test_a_network_that_never_goes_quiet_holds_the_wait_open():
    tabs = FakeTabs(ready_states=["complete"], inflight=[1])

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await navigation.settle(tabs, WaitUntil.NETWORKIDLE)


async def test_going_back_asks_for_the_entry_before_this_one():
    tabs = FakeTabs(
        current=1,
        documents=[
            ("loader-1", "https://example.com/b"),
            ("loader-2", "https://example.com/a"),
        ],
    )

    await navigation.back(tabs)

    assert ("Page.navigateToHistoryEntry", {"entryId": "a"}) in tabs.sent


async def test_going_forward_asks_for_the_entry_after_this_one():
    tabs = FakeTabs(
        current=1,
        documents=[
            ("loader-1", "https://example.com/b"),
            ("loader-2", "https://example.com/c"),
        ],
    )

    await navigation.forward(tabs)

    assert ("Page.navigateToHistoryEntry", {"entryId": "c"}) in tabs.sent


@pytest.mark.parametrize(
    ("current", "walk"), [(0, navigation.back), (2, navigation.forward)]
)
async def test_walking_off_the_end_of_the_history_says_so(current: int, walk: Any):
    tabs = FakeTabs(current=current)

    with pytest.raises(NavigationFailed, match="no such history entry"):
        await walk(tabs)


async def test_a_step_that_only_changed_the_fragment_still_counts_as_arriving():
    tabs = FakeTabs(
        current=1,
        documents=[
            ("loader-1", "https://example.com/page#install"),
            ("loader-1", "https://example.com/page"),
        ],
    )

    async with asyncio.timeout(5):
        await navigation.back(tabs)

    assert ("Page.navigateToHistoryEntry", {"entryId": "a"}) in tabs.sent


async def test_a_reload_waits_for_the_document_that_replaces_this_one():
    tabs = FakeTabs(
        documents=[
            ("loader-1", "https://example.com/"),
            ("loader-2", "https://example.com/"),
        ]
    )

    await navigation.reload(tabs)

    assert ("Page.reload", {"ignoreCache": False}) in tabs.sent


async def test_a_page_that_never_leaves_is_reported_rather_than_waited_out(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(navigation, "REPLACE_TIMEOUT", 0.1)
    tabs = FakeTabs(documents=[("loader-1", "https://example.com/")])

    with pytest.raises(NavigationFailed, match="never left"):
        await navigation.reload(tabs)
