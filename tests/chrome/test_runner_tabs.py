import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pool.protocol import EventData, TabActivated, TabClosed, TabOpened
from pool.browser import tabs as tabs_module
from pool.browser.tabs import NoActiveTab, Tabs


class FakeCdp:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}
        self.sent: list[tuple[str, dict[str, Any] | None, str | None]] = []
        self.answers: dict[str, Any] = {}
        self.attached = 0

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Any = None) -> None:
        if handler is None:
            self.listeners.pop(event, None)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append((method, params, session_id))
        if method == "Target.attachToTarget":
            self.attached += 1
            return {"sessionId": f"s{self.attached}"}
        answer: Any = self.answers.get(method, {})
        if callable(answer):
            answer = answer(params)
        return dict(answer)

    def emit(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> None:
        message = {"method": method, "params": params}
        if session_id is not None:
            message["sessionId"] = session_id
        for handler in list(self.listeners.get(method, ())):
            handler(message)

    def opened(
        self, target_id: str, session_id: str, opener: str | None = None
    ) -> None:
        info: dict[str, Any] = {
            "targetId": target_id,
            "type": "page",
            "url": f"https://example.com/{target_id}",
            "title": target_id,
        }
        if opener is not None:
            info["openerId"] = opener
        self.emit(
            "Target.attachedToTarget", {"sessionId": session_id, "targetInfo": info}
        )

    def closed(self, session_id: str) -> None:
        self.emit("Target.detachedFromTarget", {"sessionId": session_id})

    @property
    def methods(self) -> list[str]:
        return [method for method, _, _ in self.sent]


class Announcements:
    def __init__(self) -> None:
        self.seen: list[EventData] = []

    async def __call__(self, event: EventData) -> None:
        self.seen.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.seen if isinstance(event, kind)]


async def _until(condition, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise TimeoutError(f"{what} did not happen in {timeout}s")
        await asyncio.sleep(0.01)


async def _open(
    cdp: FakeCdp,
    announced: Announcements,
    target_id: str,
    session_id: str,
    opener: str | None = None,
) -> None:
    before = len(announced.of(TabOpened))
    cdp.opened(target_id, session_id, opener)
    await _until(
        lambda: len(announced.of(TabOpened)) > before, f"{target_id} to be announced"
    )


@pytest.fixture
def cdp(monkeypatch: pytest.MonkeyPatch) -> FakeCdp:
    fake = FakeCdp()

    async def pages(_port: int) -> list[dict[str, Any]]:
        return [
            {
                "id": "first",
                "type": "page",
                "url": "https://example.com/first",
                "title": "first",
            }
        ]

    monkeypatch.setattr(tabs_module.Cdp, "pages", staticmethod(pages))
    return fake


@pytest.fixture
async def started(cdp: FakeCdp) -> AsyncIterator[tuple[Tabs, Announcements]]:
    tabs = Tabs(cdp)  # pyright: ignore[reportArgumentType]
    announced = Announcements()
    tabs.on_event = announced
    await tabs.start()
    try:
        yield tabs, announced
    finally:
        await tabs.stop()


async def test_the_page_that_is_already_open_becomes_the_session(
    started: tuple[Tabs, Announcements],
):
    tabs, _ = started

    assert tabs.active.target_id == "first"
    assert [tab.target_id for tab in tabs.order] == ["first"]


async def test_the_page_the_session_starts_on_is_prepared_to_be_watched(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    for method in ("Page.enable", "Runtime.enable", "Network.enable"):
        assert method in cdp.methods


async def test_a_tab_that_opens_itself_takes_over_and_is_announced(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started

    cdp.opened("popup", "s-popup", opener="first")

    await _until(lambda: announced.of(TabOpened), "the announcement")
    assert tabs.active.target_id == "popup"
    assert announced.of(TabOpened)[0].index == 1


async def test_a_target_that_is_not_a_page_is_not_a_tab(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, _ = started

    cdp.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "s-worker",
            "targetInfo": {"targetId": "worker", "type": "service_worker"},
        },
    )

    assert [tab.target_id for tab in tabs.order] == ["first"]


async def test_the_same_target_is_not_added_twice(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")

    cdp.opened("second", "s2")
    await tabs.settled()

    assert [tab.target_id for tab in tabs.order] == ["first", "second"]
    assert len(announced.of(TabOpened)) == 1


async def test_tabs_keep_the_order_they_were_opened_in(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started

    await _open(cdp, announced, "second", "s2")
    await _open(cdp, announced, "third", "s3")

    assert [tab.target_id for tab in tabs.order] == ["first", "second", "third"]
    assert tabs.index_of("third") == 2


async def test_closing_the_tab_in_front_hands_the_session_to_its_neighbour(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")
    await _open(cdp, announced, "third", "s3")
    await tabs.activate(0)

    cdp.closed(tabs.active.session_id)

    assert tabs.active.target_id == "second"
    assert [tab.target_id for tab in tabs.order] == ["second", "third"]


async def test_closing_a_tab_in_the_middle_moves_to_the_one_after_it(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")
    await _open(cdp, announced, "third", "s3")
    await tabs.activate(1)

    cdp.closed("s2")

    assert tabs.active.target_id == "third"


async def test_closing_the_last_tab_moves_to_the_one_before_it(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")
    await _open(cdp, announced, "third", "s3")
    await tabs.activate(2)

    cdp.closed("s3")

    assert tabs.active.target_id == "second"


async def test_closing_a_tab_nobody_was_looking_at_leaves_the_session_alone(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")
    await tabs.activate(0)

    cdp.closed("s2")

    assert tabs.active.target_id == "first"


async def test_a_closed_tab_is_announced_by_the_place_it_had(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    _, announced = started
    await _open(cdp, announced, "second", "s2")

    cdp.closed("s2")

    await _until(lambda: announced.of(TabClosed), "the announcement")
    assert announced.of(TabClosed)[0].index == 1


async def test_a_session_whose_last_tab_went_has_no_active_tab(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, _ = started

    cdp.closed("s1")

    with pytest.raises(NoActiveTab):
        _ = tabs.active


async def test_activating_a_tab_announces_it(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")

    await tabs.activate(1)

    assert announced.of(TabActivated)[0].index == 1
    assert tabs.active.target_id == "second"


async def test_asking_for_a_tab_that_is_not_there_is_an_error(
    started: tuple[Tabs, Announcements],
):
    tabs, _ = started

    with pytest.raises(IndexError):
        await tabs.activate(4)


async def test_what_a_tab_says_about_itself_is_kept_up_to_date(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, _ = started

    cdp.emit(
        "Target.targetInfoChanged",
        {
            "targetInfo": {
                "targetId": "first",
                "type": "page",
                "url": "https://example.com/somewhere-else",
                "title": "somewhere else",
            }
        },
    )

    assert tabs.active.url == "https://example.com/somewhere-else"


async def test_a_snapshot_says_which_tab_is_in_front(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    cdp.answers["Runtime.evaluate"] = {"result": {"value": "the title"}}
    await _open(cdp, announced, "second", "s2")
    await tabs.activate(1)

    snapshot = await tabs.snapshot()

    assert [tab["index"] for tab in snapshot] == [0, 1]
    assert [tab["active"] for tab in snapshot] == [False, True]
    assert snapshot[0]["title"] == "the title"


async def test_a_page_waiting_on_nothing_is_quiet(
    started: tuple[Tabs, Announcements],
):
    tabs, _ = started

    assert tabs.inflight() == 0


async def test_a_request_in_flight_is_counted_until_it_finishes(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, _ = started
    session = tabs.active.session_id

    cdp.emit("Network.requestWillBeSent", {"requestId": "r1"}, session)
    cdp.emit("Network.requestWillBeSent", {"requestId": "r2"}, session)
    assert tabs.inflight() == 2

    cdp.emit("Network.loadingFinished", {"requestId": "r1"}, session)
    cdp.emit("Network.loadingFailed", {"requestId": "r2"}, session)

    assert tabs.inflight() == 0


async def test_a_request_that_never_ends_stops_counting_against_the_page(
    started: tuple[Tabs, Announcements], cdp: FakeCdp, monkeypatch: pytest.MonkeyPatch
):
    tabs, _ = started
    monkeypatch.setattr(tabs_module, "STALE_REQUEST", 0.05)

    cdp.emit("Network.requestWillBeSent", {"requestId": "r1"}, tabs.active.session_id)
    assert tabs.inflight() == 1
    await asyncio.sleep(0.1)

    assert tabs.inflight() == 0


async def test_a_request_belonging_to_another_tab_is_not_this_ones(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")
    await tabs.activate(0)

    cdp.emit("Network.requestWillBeSent", {"requestId": "r1"}, "s2")

    assert tabs.inflight() == 0


async def test_closing_a_tab_waits_until_the_browser_has_let_go_of_it(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")

    def detaching(_params: dict[str, Any] | None) -> dict[str, Any]:
        cdp.closed("s2")
        return {}

    cdp.answers["Target.closeTarget"] = detaching

    await tabs.close(1)

    assert [tab.target_id for tab in tabs.order] == ["first"]
    assert tabs.active.target_id == "first"


async def test_a_tab_the_browser_never_lets_go_of_does_not_hold_the_session(
    started: tuple[Tabs, Announcements], cdp: FakeCdp, monkeypatch: pytest.MonkeyPatch
):
    tabs, announced = started
    await _open(cdp, announced, "second", "s2")
    monkeypatch.setattr(tabs_module, "ATTACH_TIMEOUT", 0.05)

    async with asyncio.timeout(5):
        await tabs.close(1)

    assert [tab.target_id for tab in tabs.order] == ["first", "second"]


async def test_a_tab_the_session_opened_stays_in_front_of_the_one_it_replaced(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    cdp.answers["Target.createTarget"] = {"targetId": "second"}

    creating = asyncio.ensure_future(tabs.create("https://example.com/second"))
    await asyncio.sleep(0)
    cdp.opened("second", "s2")
    index = await creating

    assert index == 1
    assert tabs.active.target_id == "second"
    assert len(announced.of(TabOpened)) == 1


async def test_activating_a_tab_outlasts_the_one_that_was_still_opening(
    started: tuple[Tabs, Announcements], cdp: FakeCdp
):
    tabs, announced = started
    cdp.opened("second", "s2")

    await tabs.activate(0)

    assert tabs.active.target_id == "first"
    assert len(announced.of(TabOpened)) == 1
