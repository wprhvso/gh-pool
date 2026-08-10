import asyncio

from gh_chrome_protocol import Goto, WaitUntil
from gh_chrome_runner.cdp import CdpError
from gh_chrome_runner.tabs import Tabs

READY_STATES = {
    WaitUntil.DOMCONTENTLOADED: ("interactive", "complete"),
    WaitUntil.LOAD: ("complete",),
    WaitUntil.NETWORKIDLE: ("complete",),
}

NETWORK_IDLE_QUIET = 0.5
POLL = 0.1

QUIET_JS = """
(() => {
  const entries = performance.getEntriesByType('resource');
  const last = entries.length ? entries[entries.length - 1].responseEnd : 0;
  return performance.now() - last;
})()
"""


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
    await tabs.send("Page.reload", {"ignoreCache": False})
    await settle(tabs, WaitUntil.LOAD)


async def _history(tabs: Tabs, offset: int) -> None:
    history = await tabs.send("Page.getNavigationHistory")
    index = history["currentIndex"] + offset
    entries = history["entries"]
    if not 0 <= index < len(entries):
        raise NavigationFailed("no such history entry")
    await tabs.send("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
    await settle(tabs, WaitUntil.LOAD)


async def settle(tabs: Tabs, wait_until: WaitUntil) -> None:
    while await tabs.evaluate("document.readyState") not in READY_STATES[wait_until]:
        await asyncio.sleep(POLL)
    if wait_until is not WaitUntil.NETWORKIDLE:
        return
    while float(await tabs.evaluate(QUIET_JS) or 0) / 1000 < NETWORK_IDLE_QUIET:
        await asyncio.sleep(POLL)
