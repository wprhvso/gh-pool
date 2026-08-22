import json

import pytest

from pool.client.errors import TapError, TapRejected, TapTimeout
from pool.client.tap import SCRIPT, Captured, Rule, Tap

CAPTURE = {
    "name": "generate",
    "url": "https://example.com/generate",
    "method": "POST",
    "headers": {"authorization": "Bearer token"},
    "body": '{"prompt": "hi"}',
}


class FakeSession:
    def __init__(self, answers=None, default=None):
        self.calls = []
        self.scripts = []
        self._answers = list(answers or [])
        self._default = default

    def init_script(self, source, _timeout=None):
        self.scripts.append(source)
        return _resolved("identifier")

    def evaluate(self, expression, _timeout=None):
        self.calls.append(expression)
        if not self._answers:
            return _resolved(self._default)
        return _resolved(self._answers.pop(0))


async def _resolved(value):
    return value


def _frame(text="", status=200, error=None, done=False):
    return {"text": text, "status": status, "error": error, "done": done}


def _configured(session):
    prefix = "window.__ghTap.configure("
    call = next(item for item in session.calls if item.startswith(prefix))
    return json.loads(call[len(prefix) : -1])


def test_the_script_is_a_single_expression():
    assert SCRIPT.startswith("(() => {")
    assert SCRIPT.rstrip().endswith("})()")


async def test_arming_registers_the_script_once():
    session = FakeSession()
    tap = Tap(session)

    await tap.arm()
    await tap.arm()

    assert session.scripts == [SCRIPT]


async def test_install_seeds_the_current_document_and_the_rules():
    session = FakeSession()
    rules = [Rule(name="generate", url="/generate", action="capture", status=400)]

    await Tap(session).install(rules)

    assert session.calls[0] == SCRIPT
    assert _configured(session) == [
        {
            "name": "generate",
            "url": "/generate",
            "action": "capture",
            "method": None,
            "status": 400,
            "body": None,
            "headers": {},
        }
    ]


async def test_take_waits_across_windows_until_the_request_shows_up():
    session = FakeSession([None, None, CAPTURE])
    tap = Tap(session, window=0.01)

    captured = await tap.take("generate", timeout=5)

    assert captured.url == CAPTURE["url"]
    assert captured.headers == CAPTURE["headers"]
    assert len(session.calls) == 3


async def test_take_gives_up_with_a_timeout():
    session = FakeSession()
    tap = Tap(session, window=0.01)

    with pytest.raises(TapTimeout):
        await tap.take("generate", timeout=0.02)


async def test_replay_streams_every_chunk_and_then_stops_the_stream():
    session = FakeSession(
        ["7", _frame("first"), _frame("second"), _frame(done=True), True]
    )
    tap = Tap(session, window=0.01)

    chunks = [
        chunk async for chunk in tap.replay(Captured.model_validate(CAPTURE), timeout=5)
    ]

    assert chunks == ["first", "second"]
    assert session.calls[-1] == 'window.__ghTap.stop("7")'


async def test_replay_sends_the_overridden_body():
    session = FakeSession(["7", _frame(done=True), True])
    tap = Tap(session, window=0.01)

    async for _ in tap.replay(
        Captured.model_validate(CAPTURE), body='{"prompt": "other"}', timeout=5
    ):
        pass

    request = json.loads(session.calls[0].removeprefix("window.__ghTap.replay(")[:-1])
    assert request["body"] == '{"prompt": "other"}'
    assert request["headers"] == CAPTURE["headers"]


async def test_a_rejected_replay_carries_the_status_and_the_body():
    session = FakeSession(["7", _frame("quota", status=429, done=True), True])
    tap = Tap(session, window=0.01)

    with pytest.raises(TapRejected) as failure:
        async for _ in tap.replay(Captured.model_validate(CAPTURE), timeout=5):
            pass

    assert failure.value.status == 429
    assert failure.value.body == "quota"


async def test_a_page_side_failure_becomes_a_tap_error():
    session = FakeSession(["7", _frame(error="TypeError: failed to fetch"), True])
    tap = Tap(session, window=0.01)

    with pytest.raises(TapError, match="failed to fetch"):
        async for _ in tap.replay(Captured.model_validate(CAPTURE), timeout=5):
            pass


async def test_a_stalled_replay_times_out():
    session = FakeSession(["7"], default=_frame())
    tap = Tap(session, window=0.01)

    with pytest.raises(TapTimeout):
        async for _ in tap.replay(Captured.model_validate(CAPTURE), timeout=0.02):
            pass
