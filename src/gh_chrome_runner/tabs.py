from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gh_chrome_runner.cdp import Cdp, CdpError

log = logging.getLogger(__name__)

ATTACH_TIMEOUT = 15.0

OnOpened = Callable[[int, str, bool], Awaitable[None]]
OnClosed = Callable[[int], Awaitable[None]]
OnActivated = Callable[[int], Awaitable[None]]


@dataclass(slots=True)
class Tab:
    target_id: str
    session_id: str
    url: str = ""
    title: str = ""
    opener: str | None = None


class NoActiveTab(Exception):
    pass


class Tabs:
    def __init__(self, cdp: Cdp) -> None:
        self.cdp = cdp
        self._order: list[str] = []
        self._tabs: dict[str, Tab] = {}
        self._active: str | None = None
        self._on_opened: OnOpened | None = None
        self._on_closed: OnClosed | None = None
        self._on_activated: OnActivated | None = None
        self._events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None

    def watch(self, opened: OnOpened, closed: OnClosed, activated: OnActivated) -> None:
        self._on_opened = opened
        self._on_closed = closed
        self._on_activated = activated

    def unwatch(self) -> None:
        self._on_opened = None
        self._on_closed = None
        self._on_activated = None

    @property
    def active(self) -> Tab:
        if self._active is None or self._active not in self._tabs:
            raise NoActiveTab("session has no active tab")
        return self._tabs[self._active]

    @property
    def order(self) -> list[Tab]:
        return [self._tabs[target_id] for target_id in self._order if target_id in self._tabs]

    def index_of(self, target_id: str) -> int:
        return self._order.index(target_id)

    async def start(self) -> None:
        self.cdp.on("Target.attachedToTarget", self._attached)
        self.cdp.on("Target.detachedFromTarget", self._detached)
        self.cdp.on("Target.targetInfoChanged", self._info_changed)
        self._pump = asyncio.create_task(self._drain())
        deadline = asyncio.get_running_loop().time() + ATTACH_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if self._order:
                await self._prepare(self.active)
                return
            await asyncio.sleep(0.1)
        raise NoActiveTab("chrome did not expose a page target")

    async def stop(self) -> None:
        if self._pump is None:
            return
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pump
        self._pump = None

    def _attached(self, message: dict[str, Any]) -> None:
        params = message["params"]
        info = params["targetInfo"]
        if info["type"] != "page":
            return
        target_id = info["targetId"]
        tab = Tab(
            target_id=target_id,
            session_id=params["sessionId"],
            url=info.get("url", ""),
            title=info.get("title", ""),
            opener=info.get("openerId"),
        )
        self._tabs[target_id] = tab
        if target_id not in self._order:
            self._order.append(target_id)
        first = self._active is None
        if first or tab.opener is not None:
            self._active = target_id
        if not first:
            self._events.put_nowait(("opened", target_id))

    def _detached(self, message: dict[str, Any]) -> None:
        session_id = message["params"]["sessionId"]
        target_id = next(
            (key for key, tab in self._tabs.items() if tab.session_id == session_id), None
        )
        if target_id is None:
            return
        index = self._order.index(target_id) if target_id in self._order else None
        self._order = [item for item in self._order if item != target_id]
        self._tabs.pop(target_id, None)
        if self._active == target_id:
            self._active = self._order[-1] if self._order else None
        if index is not None:
            self._events.put_nowait(("closed", index))

    def _info_changed(self, message: dict[str, Any]) -> None:
        info = message["params"]["targetInfo"]
        tab = self._tabs.get(info["targetId"])
        if tab is None:
            return
        tab.url = info.get("url", tab.url)
        tab.title = info.get("title", tab.title)

    async def _drain(self) -> None:
        while True:
            kind, payload = await self._events.get()
            try:
                if kind == "opened":
                    await self._handle_opened(str(payload))
                elif kind == "closed" and self._on_closed is not None:
                    await self._on_closed(int(payload))
            except Exception:
                log.exception("failed to handle a tab event")

    async def _handle_opened(self, target_id: str) -> None:
        tab = self._tabs.get(target_id)
        if tab is None:
            return
        await self._prepare(tab)
        await self.bring_to_front(tab)
        if self._on_opened is not None:
            await self._on_opened(self.index_of(target_id), tab.url, True)

    async def _prepare(self, tab: Tab) -> None:
        await self.cdp.send("Page.enable", session_id=tab.session_id)
        await self.cdp.send("Runtime.enable", session_id=tab.session_id)
        await self.cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True}, tab.session_id)

    async def bring_to_front(self, tab: Tab | None = None) -> None:
        target = tab or self.active
        await self.cdp.send("Target.activateTarget", {"targetId": target.target_id})
        await self.cdp.send("Page.bringToFront", session_id=target.session_id)
        self._active = target.target_id

    async def activate(self, index: int) -> None:
        tabs = self.order
        if index >= len(tabs):
            raise IndexError(index)
        await self.bring_to_front(tabs[index])
        if self._on_activated is not None:
            await self._on_activated(index)

    async def create(self, url: str | None) -> int:
        result = await self.cdp.send("Target.createTarget", {"url": url or "about:blank"})
        target_id = result["targetId"]
        deadline = asyncio.get_running_loop().time() + ATTACH_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if target_id in self._tabs:
                await self.bring_to_front(self._tabs[target_id])
                return self.index_of(target_id)
            await asyncio.sleep(0.05)
        raise NoActiveTab("new tab did not attach")

    async def close(self, index: int) -> None:
        tabs = self.order
        if index >= len(tabs):
            raise IndexError(index)
        await self.cdp.send("Target.closeTarget", {"targetId": tabs[index].target_id})

    async def evaluate(self, expression: str, tab: Tab | None = None) -> Any:
        target = tab or self.active
        result = await self.cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            target.session_id,
        )
        details = result.get("exceptionDetails")
        if details is not None:
            raise CdpError("Runtime.evaluate", details.get("text", "evaluation failed"))
        return result.get("result", {}).get("value")

    async def navigate(self, url: str) -> None:
        tab = self.active
        result = await self.cdp.send("Page.navigate", {"url": url}, tab.session_id)
        error = result.get("errorText")
        if error:
            raise CdpError("Page.navigate", error)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.cdp.send(method, params, self.active.session_id)


async def new_tab(tabs: Tabs, args: Any) -> int:
    return await tabs.create(args.url)


async def activate(tabs: Tabs, args: Any) -> None:
    await tabs.activate(args.index)


async def close_tab(tabs: Tabs, args: Any) -> None:
    await tabs.close(args.index)


async def list_tabs(tabs: Tabs) -> list[dict[str, Any]]:
    active = tabs.active.target_id
    return [
        {"index": index, "url": tab.url, "title": tab.title, "active": tab.target_id == active}
        for index, tab in enumerate(tabs.order)
    ]
