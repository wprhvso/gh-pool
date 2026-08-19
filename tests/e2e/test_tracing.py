import httpx
from tests.e2e.stack import Stack, until

from gh_chrome_protocol import Method
from gh_chrome_protocol.trace import TraceContext

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


async def _enqueue(
    api: httpx.AsyncClient, session_id: object, **headers: str
) -> httpx.Response:
    return await api.post(
        f"/sessions/{session_id}/commands",
        json={"args": {"method": str(Method.URL)}},
        headers=headers,
    )


async def test_the_callers_trace_reaches_the_runner(
    stack: Stack, api: httpx.AsyncClient
) -> None:
    session, runner = await stack.scripted()

    response = await _enqueue(api, session.id, traceparent=PARENT, tracestate="v=1")
    assert response.status_code == httpx.codes.ACCEPTED

    await until(lambda: bool(runner.received), 15.0, "the command")
    envelope = runner.received[-1]
    assert envelope.traceparent == PARENT
    assert envelope.tracestate == "v=1"

    carried = TraceContext.parse(envelope.traceparent, envelope.tracestate)
    assert carried is not None
    assert carried.trace_id == TRACE_ID


async def test_a_command_sent_without_a_trace_still_runs(
    stack: Stack, api: httpx.AsyncClient
) -> None:
    session, runner = await stack.scripted()

    response = await _enqueue(api, session.id)
    assert response.status_code == httpx.codes.ACCEPTED

    await until(lambda: bool(runner.received), 15.0, "the command")
    assert runner.received[-1].traceparent is None
    assert runner.received[-1].tracestate is None


async def test_a_malformed_traceparent_is_dropped_rather_than_forwarded(
    stack: Stack, api: httpx.AsyncClient
) -> None:
    session, runner = await stack.scripted()

    response = await _enqueue(api, session.id, traceparent="not-a-traceparent")
    assert response.status_code == httpx.codes.ACCEPTED

    await until(lambda: bool(runner.received), 15.0, "the command")
    assert runner.received[-1].traceparent is None
