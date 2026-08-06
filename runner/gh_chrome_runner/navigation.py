from __future__ import annotations

import asyncio

from gh_chrome_protocol import WaitUntil
from gh_chrome_protocol.commands import GotoArgs

from gh_chrome_runner.cdp import CdpError
from gh_chrome_runner.tabs import Tabs

READY_STATES = {
    WaitUntil.DOMCONTENTLOADED: ("interactive", "complete"),
    WaitUntil.LOAD: ("complete",),
    WaitUntil.NETWORKIDLE: ("complete",),
}

NETWORK_IDLE_QUIET = 0.5
POLL = 0.1


class NavigationFailed(Exception):
    pass


async def goto(tabs: Tabs, args: GotoArgs) -> None:
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
    await tabs.send("Page.reload", {"ignoreCache": False})
    await settle(tabs, WaitUntil.LOAD)


async def _history(tabs: Tabs, offset: int) -> None:
    history = await tabs.send("Page.getNavigationHistory")
    index = history["currentIndex"] + offset
    entries = history["entries"]
    if index < 0 or index >= len(entries):
        raise NavigationFailed("no such history entry")
    await tabs.send("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
    await settle(tabs, WaitUntil.LOAD)


async def settle(tabs: Tabs, wait_until: WaitUntil) -> None:
    expected = READY_STATES[wait_until]
    while True:
        state = await tabs.evaluate("document.readyState")
        if state in expected:
            break
        await asyncio.sleep(POLL)
    if wait_until is WaitUntil.NETWORKIDLE:
        await _network_idle(tabs)


async def _network_idle(tabs: Tabs) -> None:
    script = """
    (() => {
      const entries = performance.getEntriesByType('resource');
      const last = entries.length ? entries[entries.length - 1].responseEnd : 0;
      return performance.now() - last;
    })()
    """
    while True:
        quiet = float(await tabs.evaluate(script) or 0)
        if quiet / 1000 >= NETWORK_IDLE_QUIET:
            return
        await asyncio.sleep(POLL)
