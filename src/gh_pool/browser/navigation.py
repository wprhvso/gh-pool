import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from gh_pool.protocol import Goto, WaitUntil
from gh_pool.browser.cdp import CdpError
from gh_pool.browser.tabs import Tabs

READY_STATES = {
    WaitUntil.DOMCONTENTLOADED: ("interactive", "complete"),
    WaitUntil.LOAD: ("complete",),
    WaitUntil.NETWORKIDLE: ("complete",),
}

NETWORK_IDLE_QUIET = 0.5
POLL = 0.1
REPLACE_TIMEOUT = 30.0


class NavigationFailed(Exception):
    pass


async def goto(tabs: Tabs, args: Goto) -> None:
    try:
        await tabs.navigate(args.url)
    except CdpError as exc:
        raise NavigationFailed(str(exc)) from exc
    await settle(tabs, args.wait_until)


async def back(tabs: Tabs) -> None:
    await _history(tabs, -1)


async def forward(tabs: Tabs) -> None:
    await _history(tabs, 1)


async def reload(tabs: Tabs) -> None:
    async with _replaced(tabs):
        await tabs.send("Page.reload", {"ignoreCache": False})
    await settle(tabs, WaitUntil.LOAD)


async def _history(tabs: Tabs, offset: int) -> None:
    history = await tabs.send("Page.getNavigationHistory")
    index = history["currentIndex"] + offset
    entries = history["entries"]
    if not 0 <= index < len(entries):
        raise NavigationFailed("no such history entry")
    async with _replaced(tabs):
        await tabs.send(
            "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}
        )
    await settle(tabs, WaitUntil.LOAD)


@asynccontextmanager
async def _replaced(tabs: Tabs) -> AsyncGenerator[None]:
    before = await _document(tabs)
    yield
    deadline = asyncio.get_running_loop().time() + REPLACE_TIMEOUT
    while await _document(tabs) == before:
        if asyncio.get_running_loop().time() >= deadline:
            raise NavigationFailed("the page never left")
        await asyncio.sleep(POLL)


async def _document(tabs: Tabs) -> str:
    frame = await tabs.send("Page.getFrameTree")
    top = frame["frameTree"]["frame"]
    loader = str(top.get("loaderId", ""))
    address = f"{top.get('url', '')}{top.get('urlFragment', '')}"
    return f"{loader} {address}"


async def settle(tabs: Tabs, wait_until: WaitUntil) -> None:
    while await tabs.evaluate("document.readyState") not in READY_STATES[wait_until]:
        await asyncio.sleep(POLL)
    if wait_until is not WaitUntil.NETWORKIDLE:
        return
    loop = asyncio.get_running_loop()
    since = loop.time()
    while True:
        if tabs.inflight():
            since = loop.time()
        elif loop.time() - since >= NETWORK_IDLE_QUIET:
            return
        await asyncio.sleep(POLL)
