from __future__ import annotations

from gh_chrome_runner.input import Input
from gh_chrome_runner.tabs import Tabs


async def test_typing_fills_the_field(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    await controls.type_into("#field", "hello world")
    assert await tabs.evaluate("document.querySelector('#field').value") == "hello world"


async def test_clear_replaces_previous_value(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    await controls.type_into("#field", "first")
    await controls.type_into("#field", "second", clear=True)
    assert await tabs.evaluate("document.querySelector('#field').value") == "second"


async def test_typing_non_latin_uses_remap(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    await controls.type_into("#field", "привет", clear=True)
    assert await tabs.evaluate("document.querySelector('#field').value") == "привет"


async def test_press_enter_submits(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    await tabs.evaluate(
        "document.querySelector('#field').addEventListener('keydown',"
        " e => { if (e.key === 'Enter') e.target.dataset.entered = 'yes'; })"
    )
    await controls.click("#field")
    await controls.press("enter")
    assert await tabs.evaluate("document.querySelector('#field').dataset.entered") == "yes"
