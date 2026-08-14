import asyncio

import httpx
import pytest
from tests.e2e.stack import Stack, expression_of

from gh_chrome_client import (
    ElementNotFound,
    GhChromeError,
    SessionDead,
    SessionStatus,
    Speed,
    TooManySessions,
)
from gh_chrome_protocol import ErrorCode, Method


async def _status(api: httpx.AsyncClient, session_id: object) -> str:
    response = await api.get(f"/sessions/{session_id}")
    assert response.status_code == 200
    return str(response.json()["status"])


async def test_a_new_session_dispatches_the_workflow_and_waits(
    stack: Stack, api: httpx.AsyncClient
):
    session = await stack.session()

    assert stack.server.dispatched == [session.id]
    assert await _status(api, session.id) == SessionStatus.PENDING


async def test_the_runner_connecting_makes_the_session_active(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    assert await _status(api, session.id) == SessionStatus.ACTIVE
    assert session.alive
    assert not session.state_stale


async def test_a_failed_dispatch_leaves_no_session_behind(
    stack: Stack, api: httpx.AsyncClient
):
    stack.server.dispatch_error = "no such workflow"

    with pytest.raises(GhChromeError, match="502"):
        await stack.session()

    dispatched = stack.server.dispatched[-1]
    assert await _status(api, dispatched) == SessionStatus.DEAD


async def test_the_parameters_reach_the_runner_unchanged(stack: Stack):
    session, runner = await stack.scripted(
        width=1280, height=720, fps=8, mouse_speed=Speed.FAST
    )

    assert runner.config is not None
    assert runner.config.params.width == 1280
    assert runner.config.params.height == 720
    assert runner.config.params.fps == 8
    assert runner.config.params.mouse_speed is Speed.FAST
    assert session.params.mouse_speed is Speed.FAST


async def test_commands_are_numbered_and_answered_in_order(stack: Stack):
    session, runner = await stack.scripted()
    runner.on(Method.EVAL, expression_of)

    answers = [await session.evaluate(f"page-{index}") for index in range(3)]

    assert answers == ["page-0", "page-1", "page-2"]
    assert [envelope.seq for envelope in runner.received] == [1, 2, 3]


async def test_a_batch_submitted_at_once_gives_every_caller_its_own_answer(
    stack: Stack,
):
    """Five commands in flight together, and nobody gets somebody else's reply.

    Sorting the answers hid exactly that. Five submissions race each other to
    the server and the sequence numbers go out in whatever order the requests
    arrive — but each of the five results has to find its way back to the
    caller that asked for it, and each command has to be numbered once.
    """
    session, runner = await stack.scripted()
    runner.on(Method.EVAL, expression_of)

    queued = [session.evaluate(f"page-{index}") for index in range(5)]
    answers = [await command for command in queued]

    assert answers == [f"page-{index}" for index in range(5)]
    assert [envelope.seq for envelope in runner.received] == [1, 2, 3, 4, 5]
    assert sorted(expression_of(envelope) for envelope in runner.received) == answers


async def test_two_sessions_at_once_do_not_cross_wires(stack: Stack):
    first, first_runner = await stack.scripted()
    second, second_runner = await stack.scripted()
    first_runner.returns(Method.TITLE, "the first page")
    second_runner.returns(Method.TITLE, "the second page")

    assert await first.title() == "the first page"
    assert await second.title() == "the second page"
    assert len(first_runner.received) == 1
    assert len(second_runner.received) == 1


async def test_a_result_with_a_nul_in_it_still_comes_back(stack: Stack):
    session, runner = await stack.scripted()
    runner.returns(Method.TITLE, "a\x00page")
    runner.returns(Method.EVAL, {"keys": ["\x00", "b"], "nested": {"c": "d\x00"}})

    assert await session.title() == "a\ufffdpage"
    assert await session.evaluate("anything") == {
        "keys": ["\ufffd", "b"],
        "nested": {"c": "d\ufffd"},
    }


async def test_a_runner_failure_arrives_as_the_matching_exception(stack: Stack):
    session, runner = await stack.scripted()
    runner.raises(Method.CLICK, ErrorCode.NOT_FOUND, "#nope was never there")

    with pytest.raises(ElementNotFound, match="#nope"):
        await session.click("#nope")


async def test_closing_the_session_tells_the_runner_and_settles_the_state(
    stack: Stack, api: httpx.AsyncClient
):
    session, runner = await stack.scripted()

    await session.close()

    await runner.wait_for_close()
    assert runner.closed
    assert await _status(api, session.id) == SessionStatus.CLOSED
    assert not session.alive


async def test_a_closed_session_refuses_further_commands(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()
    await session.close()

    refused = await api.post(
        f"/sessions/{session.id}/commands",
        json={"args": {"method": "title"}, "timeout": None},
    )

    assert refused.status_code == 409


async def test_a_command_queued_after_the_end_fails_the_caller(stack: Stack):
    session, _ = await stack.scripted()
    await session.close()

    with pytest.raises(SessionDead):
        await session.title()


async def test_the_parallel_limit_turns_the_next_session_away(stack: Stack):
    await stack.scripted(max_parallel=1)

    with pytest.raises(TooManySessions):
        await stack.session(max_parallel=1)


async def test_the_parallel_limit_holds_when_the_requests_arrive_together(
    stack: Stack,
):
    await stack.scripted(max_parallel=2)

    asked = await asyncio.gather(
        *(stack.session(max_parallel=2) for _ in range(4)), return_exceptions=True
    )

    made = [item for item in asked if not isinstance(item, BaseException)]
    refused = [item for item in asked if isinstance(item, TooManySessions)]
    assert len(made) == 1
    assert len(refused) == 3


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-the-secret"}, {"Authorization": "Basic nope"}],
)
async def test_the_api_is_closed_without_the_token(
    stack: Stack, headers: dict[str, str]
):
    async with httpx.AsyncClient(base_url=stack.server.url, timeout=30.0) as anonymous:
        response = await anonymous.post("/sessions", json={}, headers=headers)

    assert response.status_code in {401, 403}
    assert stack.server.dispatched == []


async def test_a_finished_session_can_be_deleted_but_a_live_one_cannot(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    assert (await api.delete(f"/sessions/{session.id}")).status_code == 409

    await session.close()

    assert (await api.delete(f"/sessions/{session.id}")).status_code == 204
    assert (await api.get(f"/sessions/{session.id}")).status_code == 404


@pytest.mark.parametrize(
    "bitrate", ["2M -f lavfi", "$(id)", "2M;rm -rf /", "-i", "", "2 M"]
)
async def test_a_bitrate_that_is_not_one_never_reaches_the_recorder(
    stack: Stack, api: httpx.AsyncClient, bitrate: str
):
    """The value goes straight into ffmpeg's argv on the runner.

    It is the one session parameter that is a string rather than a number, so
    it is held to the shape of a bitrate at the door.
    """
    refused = await api.post("/sessions", json={"params": {"bitrate": bitrate}})

    assert refused.status_code == 422
    assert stack.server.dispatched == []


async def test_a_bitrate_that_is_one_is_taken(stack: Stack):
    _, runner = await stack.scripted(bitrate="750k")

    assert runner.config is not None
    assert runner.config.params.bitrate == "750k"


async def test_an_unknown_session_is_a_404(api: httpx.AsyncClient):
    response = await api.get("/sessions/2f1c9f38-6d0f-4a63-9e33-2f8a3f3f6b21")

    assert response.status_code == 404
