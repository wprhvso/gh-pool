from __future__ import annotations

from typing import Any

from gh_chrome_runner.locate import ElementMissing, js_string
from gh_chrome_runner.tabs import Tabs

MISSING = "__gh_chrome_missing__"


async def text(tabs: Tabs, selector: str) -> str:
    value = await tabs.evaluate(
        f"(document.querySelector({js_string(selector)}) || {{}}).innerText ?? {js_string(MISSING)}"
    )
    return _present(value, selector)


async def html(tabs: Tabs, selector: str | None) -> str:
    if selector is None:
        return str(await tabs.evaluate("document.documentElement.outerHTML"))
    value = await tabs.evaluate(
        f"(document.querySelector({js_string(selector)}) || {{}}).outerHTML ?? {js_string(MISSING)}"
    )
    return _present(value, selector)


async def attr(tabs: Tabs, selector: str, name: str) -> str | None:
    script = f"""
    (() => {{
      const el = document.querySelector({js_string(selector)});
      if (!el) return {js_string(MISSING)};
      return el.getAttribute({js_string(name)});
    }})()
    """
    value = await tabs.evaluate(script)
    if value == MISSING:
        raise ElementMissing(selector)
    return None if value is None else str(value)


async def value(tabs: Tabs, selector: str) -> str:
    result = await tabs.evaluate(
        f"(document.querySelector({js_string(selector)}) || {{}}).value ?? {js_string(MISSING)}"
    )
    return _present(result, selector)


async def url(tabs: Tabs) -> str:
    return str(await tabs.evaluate("location.href"))


async def title(tabs: Tabs) -> str:
    return str(await tabs.evaluate("document.title"))


async def evaluate(tabs: Tabs, expression: str) -> Any:
    return await tabs.evaluate(f"(() => ({expression}))()")


async def screenshot(tabs: Tabs) -> str:
    result = await tabs.send("Page.captureScreenshot", {"format": "png"})
    return str(result["data"])


def _present(value: Any, selector: str) -> str:
    if value == MISSING or value is None:
        raise ElementMissing(selector)
    return str(value)
