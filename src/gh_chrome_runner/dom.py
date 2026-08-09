"""Reading the page, and waiting for it to say what we want to hear."""

import asyncio
import re
from typing import Any

from gh_chrome_protocol import ElementState, WaitUntil
from gh_chrome_runner.locate import ElementMissing, Locator
from gh_chrome_runner.locate import js_string as js
from gh_chrome_runner.navigation import settle
from gh_chrome_runner.tabs import Tabs

POLL = 0.1
MISSING = "__gh_chrome_missing__"

ATTR_JS = """
(() => {
  const el = document.querySelector(%s);
  return el ? el.getAttribute(%s) : %s;
})()
"""


async def text(tabs: Tabs, selector: str) -> str:
    return await _property(tabs, selector, "innerText")


async def value(tabs: Tabs, selector: str) -> str:
    return await _property(tabs, selector, "value")


async def html(tabs: Tabs, selector: str | None) -> str:
    if selector is None:
        return str(await tabs.evaluate("document.documentElement.outerHTML"))
    return await _property(tabs, selector, "outerHTML")


async def attr(tabs: Tabs, selector: str, name: str) -> str | None:
    found = await tabs.evaluate(ATTR_JS % (js(selector), js(name), js(MISSING)))
    if found == MISSING:
        raise ElementMissing(selector)
    return None if found is None else str(found)


async def url(tabs: Tabs) -> str:
    return str(await tabs.evaluate("location.href") or "")


async def title(tabs: Tabs) -> str:
    return str(await tabs.evaluate("document.title"))


async def evaluate(tabs: Tabs, expression: str) -> Any:
    return await tabs.evaluate(f"(() => ({expression}))()")


async def screenshot(tabs: Tabs) -> str:
    result = await tabs.send("Page.captureScreenshot", {"format": "png"})
    return str(result["data"])


async def _property(tabs: Tabs, selector: str, name: str) -> str:
    found = await tabs.evaluate(
        f"(document.querySelector({js(selector)}) || {{}}).{name} ?? {js(MISSING)}"
    )
    if found is None or found == MISSING:
        raise ElementMissing(selector)
    return str(found)


async def wait_for(tabs: Tabs, selector: str, state: ElementState) -> None:
    if state is ElementState.ATTACHED:
        while not await tabs.evaluate(f"document.querySelector({js(selector)}) !== null"):
            await asyncio.sleep(POLL)
        return
    locator = Locator(tabs)
    while await locator.box(selector) is None:
        await asyncio.sleep(POLL)


async def wait_for_hidden(tabs: Tabs, selector: str) -> None:
    locator = Locator(tabs)
    while await locator.box(selector) is not None:
        await asyncio.sleep(POLL)


async def wait_for_url(tabs: Tabs, pattern: str) -> None:
    matcher = re.compile(pattern)
    while not matcher.search(await url(tabs)):
        await asyncio.sleep(POLL)


async def wait_for_load(tabs: Tabs, wait_until: WaitUntil) -> None:
    await settle(tabs, wait_until)


async def wait_for_function(tabs: Tabs, expression: str) -> None:
    while not await tabs.evaluate(f"Boolean({expression})"):
        await asyncio.sleep(POLL)
