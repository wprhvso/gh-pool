from typing import Any

import pytest
from tests.chrome.e2e.site import Site
from tests.chrome.e2e.stack import Stack

from pool.client import (
    ElementIntercepted,
    ElementNotFound,
    GhChromeError,
    Session,
    Speed,
)

pytestmark = pytest.mark.browser


async def _trace(session: Session, kind: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = await session.evaluate("window.trace")
    return [event for event in events if event["type"] == kind]


async def _forget(session: Session) -> None:
    await session.evaluate("(window.trace.length = 0, true)")


async def _display(session: Session, selector: str) -> str:
    return await session.evaluate(
        f"getComputedStyle(document.querySelector('{selector}')).display"
    )


async def _box(session: Session, selector: str) -> dict[str, float]:
    return await session.evaluate(
        f"(() => {{ const r = document.querySelector('{selector}')"
        ".getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; })()"
    )


async def test_a_click_is_a_real_cursor_landing_inside_the_element(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    await live.click("#save")

    box = await _box(live, "#save")
    clicks = await _trace(live, "click")
    assert len(clicks) == 1
    assert clicks[0]["target"] == "save"
    assert clicks[0]["trusted"] is True
    assert box["x"] <= clicks[0]["x"] <= box["x"] + box["w"]
    assert box["y"] <= clicks[0]["y"] <= box["y"] + box["h"]
    assert await _display(live, "#saved") == "block"


async def test_the_cursor_travels_to_what_it_clicks(live: Session, site: Site):
    await live.goto(site.url("/form"))
    await _forget(live)

    await live.click("#save")

    moves = await _trace(live, "mousemove")
    assert len({(move["x"], move["y"]) for move in moves}) > 5
    assert (moves[0]["x"], moves[0]["y"]) != (moves[-1]["x"], moves[-1]["y"])


async def test_an_instant_mouse_goes_straight_there(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live(mouse_speed=Speed.INSTANT)
    await session.goto(site.url("/form"))
    await _forget(session)

    await session.click("#save")

    moves = await _trace(session, "mousemove")
    assert len({(move["x"], move["y"]) for move in moves}) <= 2
    assert len(await _trace(session, "click")) == 1


async def test_a_double_click_arrives_as_one(live: Session, site: Site):
    await live.goto(site.url("/form"))

    await live.dblclick("#save")

    doubles = await _trace(live, "dblclick")
    assert len(doubles) == 1
    assert doubles[0]["detail"] == 2


async def test_the_right_button_opens_the_page_menu(live: Session, site: Site):
    await live.goto(site.url("/form"))

    await live.right_click("#save")

    assert len(await _trace(live, "contextmenu")) == 1
    assert await _display(live, "#menu") == "block"


async def test_hovering_moves_the_cursor_without_pressing_anything(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    await live.hover("#save")

    moves = await _trace(live, "mousemove")
    box = await _box(live, "#save")
    assert moves
    assert box["x"] <= moves[-1]["x"] <= box["x"] + box["w"]
    assert await _trace(live, "click") == []


async def test_typing_lands_in_the_field_that_was_clicked(live: Session, site: Site):
    await live.goto(site.url("/form"))

    await live.type("#name", "Ada Lovelace, 1843!")

    assert await live.value("#name") == "Ada Lovelace, 1843!"
    assert await live.evaluate("document.activeElement.id") == "name"
    typed = [
        event["key"]
        for event in await _trace(live, "keydown")
        if event["key"] != "Shift"
    ]
    assert "".join(typed) == "Ada Lovelace, 1843!"


async def test_typing_with_no_pause_at_all_still_lands_every_character(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live(type_speed=Speed.INSTANT)
    await session.goto(site.url("/form"))

    await session.type("#name", "nothing at all between these, not one pause!")

    assert (
        await session.value("#name") == "nothing at all between these, not one pause!"
    )


async def test_a_character_the_layout_has_no_key_for_is_typed_anyway(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    await live.type("#name", "café 中")

    assert await live.value("#name") == "café 中"


async def test_typing_over_a_field_clears_what_was_there(live: Session, site: Site):
    await live.goto(site.url("/form"))
    await live.type("#name", "first attempt")

    await live.type("#name", "second attempt", clear=True)

    assert await live.value("#name") == "second attempt"


async def test_a_key_press_reaches_the_page(live: Session, site: Site):
    await live.goto(site.url("/form"))
    await live.click("#name")

    await live.press("enter")

    assert [event["key"] for event in await _trace(live, "keydown")] == ["Enter"]


async def test_a_hotkey_arrives_with_its_modifier_held(live: Session, site: Site):
    await live.goto(site.url("/form"))
    await live.type("#name", "select all of this")

    await live.hotkey("ctrl", "a")

    held = [event for event in await _trace(live, "keydown") if event["ctrl"]]
    assert [event["key"] for event in held] == ["Control", "a"]
    selected: int = await live.evaluate("document.querySelector('#name').selectionEnd")
    assert selected == len("select all of this")


async def test_selecting_an_option_changes_the_value_and_tells_the_page(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    await live.select("#colour", "blue")

    assert await live.value("#colour") == "blue"
    assert [event["target"] for event in await _trace(live, "change")] == ["colour"]


async def test_selecting_an_option_the_page_does_not_have_says_so(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    with pytest.raises(ElementNotFound, match="green"):
        await live.select("#colour", "green")

    assert await live.value("#colour") == "red"
    assert await _trace(live, "change") == []


async def test_a_hotkey_with_a_key_the_layout_lacks_leaves_nothing_held(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))
    await live.click("#name")

    with pytest.raises(GhChromeError):
        await live.hotkey("ctrl", "not-a-key")

    await live.type("#name", "plain")
    assert await live.value("#name") == "plain"
    assert [event for event in await _trace(live, "keydown") if event["ctrl"]] == []


async def test_the_wheel_scrolls_the_page(live: Session, site: Site):
    await live.goto(site.url("/tall"))

    await live.scroll_by(600)

    assert await live.evaluate("window.scrollY") > 300
    assert await _trace(live, "wheel")


async def test_scrolling_to_an_element_brings_it_within_reach(
    live: Session, site: Site
):
    await live.goto(site.url("/tall"))

    await live.scroll_to("#deep")
    await live.click("#deep")

    box = await _box(live, "#deep")
    viewport: float = await live.evaluate("window.innerHeight")
    assert 0 <= box["y"] <= viewport - box["h"]
    assert await live.text("#offset") != "0"


async def test_an_element_under_a_veil_refuses_the_click(live: Session, site: Site):
    await live.goto(site.url("/overlay"))

    with pytest.raises(ElementIntercepted):
        await live.click("#covered", timeout=30)


async def test_a_click_on_nothing_says_so(live: Session, site: Site):
    await live.goto(site.url("/form"))

    with pytest.raises(TimeoutError):
        await live.click("#not-a-thing", timeout=5)
