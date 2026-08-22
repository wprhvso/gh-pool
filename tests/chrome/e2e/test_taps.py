import asyncio
import json

import pytest
from tests.e2e.site import Site

from gh_chrome_client import Rule, Session, Tap

pytestmark = pytest.mark.browser

SEND = "document.querySelector('#answer').textContent"


async def _answer(session: Session) -> str:
    await session.wait_for_function(f"{SEND}.length > 0", timeout=30)
    return await session.text("#answer")


async def test_a_rule_answers_the_page_without_touching_the_network(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [
            Rule(
                name="echo",
                url="/api/echo",
                action="fulfill",
                body='{"echoed": "never left the tab"}',
            )
        ]
    )

    await live.click("#send")

    assert await _answer(live) == '200:{"echoed": "never left the tab"}'
    assert site.hits["/api/echo"] == 0


async def test_a_rule_can_answer_a_request_made_with_xhr(live: Session, site: Site):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [Rule(name="echo", url="/api/echo", action="fulfill", body='{"from": "xhr"}')]
    )

    await live.click("#xhr")

    assert await _answer(live) == '200:{"from": "xhr"}'
    assert site.hits["/api/echo"] == 0


async def test_a_rewritten_request_reaches_the_site_with_the_new_body(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [
            Rule(
                name="echo",
                url="/api/echo",
                action="rewrite",
                body='{"said": "something else"}',
            )
        ]
    )

    await live.click("#send")

    await _answer(live)
    assert site.hits["/api/echo"] == 1
    assert site.posted[-1]["body"] == '{"said": "something else"}'


async def test_a_captured_request_is_handed_over_whole(live: Session, site: Site):
    await live.goto(site.url("/net"))
    tap = Tap(live, window=2.0)
    await tap.install(
        [Rule(name="echo", url="/api/echo", action="capture", status=418)]
    )

    await live.click("#send")
    captured = await tap.take("echo", timeout=30)

    assert captured.url == site.url("/api/echo")
    assert captured.method == "POST"
    assert captured.headers["x-marker"] == "from-the-page"
    assert json.loads(captured.body or "") == {"said": "hello"}
    assert (await _answer(live)).startswith("418:")
    assert site.hits["/api/echo"] == 0


async def test_a_captured_request_can_be_replayed_chunk_by_chunk(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live, window=2.0)
    await tap.install(
        [Rule(name="stream", url="/api/stream", action="capture", status=204)]
    )
    await live.click("#stream")
    captured = await tap.take("stream", timeout=30)

    chunks = [chunk async for chunk in tap.replay(captured, timeout=60)]

    assert "".join(chunks) == "chunk-0;chunk-1;chunk-2;chunk-3;"
    assert site.hits["/api/stream"] == 1


async def test_the_rules_are_still_there_on_the_next_document(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [Rule(name="echo", url="/api/echo", action="fulfill", body='{"still": "here"}')]
    )

    await live.reload()
    await live.click("#send")

    assert await _answer(live) == '200:{"still": "here"}'
    assert site.hits["/api/echo"] == 0


async def test_two_requests_under_one_rule_are_both_kept(live: Session, site: Site):
    await live.goto(site.url("/net"))
    tap = Tap(live, window=2.0)
    await tap.install(
        [Rule(name="echo", url="/api/echo", action="capture", status=418)]
    )

    await live.click("#send")
    await _answer(live)
    await live.click("#send")

    first = await tap.take("echo", timeout=30)
    second = await tap.take("echo", timeout=30)
    assert json.loads(first.body or "") == {"said": "hello"}
    assert json.loads(second.body or "") == {"said": "hello"}
    assert site.hits["/api/echo"] == 0


async def test_a_rule_header_is_matched_however_it_is_spelled(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [
            Rule(
                name="echo",
                url="/api/echo",
                action="fulfill",
                body="<p>from the rule</p>",
                headers={"Content-Type": "text/html", "X-Canned": "yes"},
            )
        ]
    )

    await live.click("#send")

    await _answer(live)
    assert await live.text("#headers") == "text/html"


async def test_an_xhr_answered_from_a_rule_sees_every_header_of_it(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [
            Rule(
                name="echo",
                url="/api/echo",
                action="fulfill",
                body="<p>from the rule</p>",
                headers={"Content-Type": "text/html", "X-Canned": "yes"},
            )
        ]
    )

    await live.click("#xhr")

    await _answer(live)
    assert await live.text("#headers") == "text/html|yes"


async def test_an_xhr_capture_carries_the_type_the_browser_would_have_sent(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live, window=2.0)
    await tap.install(
        [Rule(name="echo", url="/api/echo", action="capture", status=204)]
    )

    await live.click("#plain")
    captured = await tap.take("echo", timeout=30)

    assert captured.body == "said=by a plain xhr"
    assert captured.headers["content-type"] == "text/plain;charset=UTF-8"


async def test_an_xhr_used_twice_is_not_left_holding_the_canned_answer(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live)
    await tap.install(
        [Rule(name="echo", url="/api/echo", action="fulfill", body='{"canned": true}')]
    )

    await live.click("#again")

    answer = await _answer(live)
    assert "canned" not in answer
    assert json.loads(answer.removeprefix("200:"))["echoed"] == "the second one"
    assert site.hits["/api/elsewhere"] == 1


async def test_a_replay_the_caller_walked_away_from_is_stopped(
    live: Session, site: Site
):
    await live.goto(site.url("/net"))
    tap = Tap(live, window=2.0)
    await tap.install(
        [Rule(name="slow", url="/api/stream", action="capture", status=204)]
    )
    await live.click("#slow")
    captured = await tap.take("slow", timeout=30)

    async for chunk in tap.replay(captured, timeout=120):
        assert chunk.startswith("chunk-0;")
        break

    await asyncio.sleep(8.0)
    assert site.finished == 0


async def test_arming_puts_the_hooks_in_before_the_page_runs(live: Session, site: Site):
    tap = Tap(live)
    await tap.arm()

    await live.goto(site.url("/net"))

    assert await live.evaluate("Boolean(window.__ghTap)") is True
