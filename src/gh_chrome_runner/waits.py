from __future__ import annotations

import asyncio
import re

from gh_chrome_protocol import ElementState, WaitUntil

from gh_chrome_runner.locate import Locator, js_string
from gh_chrome_runner.navigation import settle
from gh_chrome_runner.tabs import Tabs

POLL = 0.1


async def wait_for(tabs: Tabs, selector: str, state: ElementState) -> None:
    locator = Locator(tabs.cdp, tabs)
    if state is ElementState.ATTACHED:
        script = f"document.querySelector({js_string(selector)}) !== null"
        while not await tabs.evaluate(script):
            await asyncio.sleep(POLL)
        return
    while await locator.box(selector) is None:
        await asyncio.sleep(POLL)


async def wait_for_hidden(tabs: Tabs, selector: str) -> None:
    locator = Locator(tabs.cdp, tabs)
    while await locator.box(selector) is not None:
        await asyncio.sleep(POLL)


async def wait_for_url(tabs: Tabs, pattern: str) -> None:
    matcher = re.compile(pattern)
    while True:
        url = str(await tabs.evaluate("location.href") or "")
        if matcher.search(url):
            return
        await asyncio.sleep(POLL)


async def wait_for_load(tabs: Tabs, wait_until: WaitUntil) -> None:
    await settle(tabs, wait_until)


async def wait_for_function(tabs: Tabs, expression: str) -> None:
    while not await tabs.evaluate(f"Boolean({expression})"):
        await asyncio.sleep(POLL)
