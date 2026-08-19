import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gh_chrome_protocol import EventData, TabActivated, TabClosed, TabOpened
from gh_chrome_runner.cdp import Cdp, CdpError
from gh_chrome_runner.config import settings

log = logging.getLogger(__name__)

ATTACH_TIMEOUT = 15.0
STALE_REQUEST = 10.0


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
        self.on_event: Callable[[EventData], Awaitable[None]] | None = None
        self._order: list[str] = []
        self._tabs: dict[str, Tab] = {}
        self._active: str | None = None
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None
        self._inflight: dict[str, dict[str, float]] = {}

    @property
    def active(self) -> Tab:
        if self._active is None or self._active not in self._tabs:
            raise NoActiveTab("session has no active tab")
        return self._tabs[self._active]

    @property
    def order(self) -> list[Tab]:
        return [
            self._tabs[target_id]
            for target_id in self._order
            if target_id in self._tabs
        ]

    def index_of(self, target_id: str) -> int:
        return self._order.index(target_id)

    async def snapshot(self) -> list[dict[str, Any]]:
        active = self.active.target_id
        return [
            {
                "index": index,
                "url": tab.url,
                "title": await self._title(tab),
                "active": tab.target_id == active,
            }
            for index, tab in enumerate(self.order)
        ]

    async def _title(self, tab: Tab) -> str:
        with contextlib.suppress(Exception):
            tab.title = str(await self.evaluate("document.title", tab))
        return tab.title

    async def start(self) -> None:
        self.cdp.on("Target.attachedToTarget", self._attached)
        self.cdp.on("Target.detachedFromTarget", self._detached)
        self.cdp.on("Target.targetInfoChanged", self._info_changed)
        self.cdp.on("Network.requestWillBeSent", self._request_began)
        self.cdp.on("Network.loadingFinished", self._request_ended)
        self.cdp.on("Network.loadingFailed", self._request_ended)
        self._pump = asyncio.create_task(self._drain())
        deadline = asyncio.get_running_loop().time() + ATTACH_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await self._adopt_existing()
            if self._order:
                await self._prepare(self.active)
                return
            await asyncio.sleep(0.2)
        raise NoActiveTab("chrome did not expose a page target")

    async def stop(self) -> None:
        if self._pump is None:
            return
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pump
        self._pump = None

    async def create(self, url: str | None) -> int:
        result = await self.cdp.send(
            "Target.createTarget", {"url": url or "about:blank"}
        )
        target_id = result["targetId"]
        deadline = asyncio.get_running_loop().time() + ATTACH_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if target_id in self._tabs:
                await self.bring_to_front(self._tabs[target_id])
                return self.index_of(target_id)
            await asyncio.sleep(0.05)
        raise NoActiveTab("new tab did not attach")

    async def activate(self, index: int) -> None:
        await self.bring_to_front(self._at(index))
        await self._emit(TabActivated(index=index))

    async def close(self, index: int) -> None:
        await self.cdp.send(
            "Target.closeTarget", {"targetId": self._at(index).target_id}
        )

    async def bring_to_front(self, tab: Tab | None = None) -> None:
        target = tab or self.active
        await self.cdp.send("Target.activateTarget", {"targetId": target.target_id})
        await self.cdp.send("Page.bringToFront", session_id=target.session_id)
        self._active = target.target_id

    async def navigate(self, url: str) -> None:
        result = await self.cdp.send(
            "Page.navigate", {"url": url}, self.active.session_id
        )
        if error := result.get("errorText"):
            raise CdpError("Page.navigate", error)

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

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.cdp.send(method, params, self.active.session_id)

    def _at(self, index: int) -> Tab:
        tabs = self.order
        if index >= len(tabs):
            raise IndexError(index)
        return tabs[index]

    async def _adopt_existing(self) -> None:
        try:
            pages = await Cdp.pages(settings.debug_port)
        except Exception as exc:
            log.warning("could not list pages: %s", exc)
            return
        for info in pages:
            target_id = info.get("id")
            if not isinstance(target_id, str) or target_id in self._tabs:
                continue
            try:
                attached = await self.cdp.send(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )
            except CdpError as exc:
                log.warning("could not attach to %s: %s", target_id, exc)
                continue
            session_id = attached.get("sessionId")
            if isinstance(session_id, str):
                self._add(
                    Tab(
                        target_id=target_id,
                        session_id=session_id,
                        url=info.get("url", ""),
                        title=info.get("title", ""),
                    )
                )

    def _add(self, tab: Tab) -> bool:
        self._tabs[tab.target_id] = tab
        if tab.target_id not in self._order:
            self._order.append(tab.target_id)
        first = self._active is None
        if first or tab.opener is not None:
            self._active = tab.target_id
        return first

    def _attached(self, message: dict[str, Any]) -> None:
        params = message["params"]
        info = params["targetInfo"]
        if info["type"] != "page" or info["targetId"] in self._tabs:
            return
        tab = Tab(
            target_id=info["targetId"],
            session_id=params["sessionId"],
            url=info.get("url", ""),
            title=info.get("title", ""),
            opener=info.get("openerId"),
        )
        if not self._add(tab):
            self._queue.put_nowait(("opened", tab.target_id))

    def _detached(self, message: dict[str, Any]) -> None:
        session_id = message["params"]["sessionId"]
        target_id = next(
            (key for key, tab in self._tabs.items() if tab.session_id == session_id),
            None,
        )
        if target_id is None:
            return
        index = self._order.index(target_id) if target_id in self._order else None
        self._order = [item for item in self._order if item != target_id]
        self._tabs.pop(target_id, None)
        self._inflight.pop(session_id, None)
        if self._active == target_id:
            self._active = self._neighbour(index)
            if self._active is not None:
                self._queue.put_nowait(("took-over", self._active))
        if index is not None:
            self._queue.put_nowait(("closed", index))

    def _neighbour(self, index: int | None) -> str | None:
        if not self._order:
            return None
        if index is None:
            return self._order[-1]
        return self._order[min(index, len(self._order) - 1)]

    def inflight(self, tab: Tab | None = None) -> int:
        target = tab or self.active
        now = asyncio.get_running_loop().time()
        started = self._inflight.get(target.session_id, {})
        return sum(1 for at in started.values() if now - at < STALE_REQUEST)

    def _request_began(self, message: dict[str, Any]) -> None:
        session_id = message.get("sessionId")
        if isinstance(session_id, str):
            self._inflight.setdefault(session_id, {})[
                message["params"]["requestId"]
            ] = asyncio.get_running_loop().time()

    def _request_ended(self, message: dict[str, Any]) -> None:
        session_id = message.get("sessionId")
        if isinstance(session_id, str):
            self._inflight.get(session_id, {}).pop(message["params"]["requestId"], None)

    def _info_changed(self, message: dict[str, Any]) -> None:
        info = message["params"]["targetInfo"]
        tab = self._tabs.get(info["targetId"])
        if tab is None:
            return
        tab.url = info.get("url", tab.url)
        tab.title = info.get("title", tab.title)

    async def _drain(self) -> None:
        while True:
            kind, payload = await self._queue.get()
            try:
                if kind == "opened":
                    await self._handle_opened(str(payload))
                elif kind == "took-over":
                    await self._show(str(payload))
                else:
                    await self._emit(TabClosed(index=int(payload)))
            except Exception:
                log.exception("failed to handle a tab event")

    async def _show(self, target_id: str) -> None:
        tab = self._tabs.get(target_id)
        if tab is not None:
            await self.bring_to_front(tab)

    async def _handle_opened(self, target_id: str) -> None:
        tab = self._tabs.get(target_id)
        if tab is None:
            return
        await self._prepare(tab)
        await self.bring_to_front(tab)
        await self._emit(
            TabOpened(index=self.index_of(target_id), url=tab.url, active=True)
        )

    async def _prepare(self, tab: Tab) -> None:
        await self.cdp.send("Page.enable", session_id=tab.session_id)
        await self.cdp.send("Runtime.enable", session_id=tab.session_id)
        await self.cdp.send("Network.enable", session_id=tab.session_id)
        await self.cdp.send(
            "Page.setLifecycleEventsEnabled", {"enabled": True}, tab.session_id
        )

    async def _emit(self, event: EventData) -> None:
        if self.on_event is not None:
            await self.on_event(event)
