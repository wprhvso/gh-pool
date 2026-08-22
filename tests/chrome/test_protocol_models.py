import json
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from pool.protocol import (
    MAX_TIMEOUT,
    Bare,
    CommandEnvelope,
    CommandRequest,
    Event,
    EventData,
    EventType,
    Goto,
    Method,
    ScrollBy,
    SessionCreate,
    SessionParams,
    SessionStatus,
    Speed,
    TabOpened,
    TypeText,
    WaitUntil,
)

COMMAND_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"


def _request(**args: object) -> CommandRequest:
    return CommandRequest.model_validate({"args": args})


def test_a_session_asked_for_with_nothing_gets_the_defaults():
    params = SessionParams()

    assert (params.width, params.height, params.fps) == (1920, 1080, 15)
    assert params.bitrate == "2M"
    assert params.timeout == 30.0
    assert params.mouse_speed is Speed.NORMAL
    assert params.subscribe == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 319),
        ("width", 3841),
        ("height", 239),
        ("height", 2161),
        ("fps", 0),
        ("fps", 61),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", MAX_TIMEOUT + 1),
    ],
)
def test_a_parameter_outside_what_the_runner_can_do_is_refused(
    field: str, value: object
):
    with pytest.raises(ValidationError):
        SessionParams(**{field: value})


@pytest.mark.parametrize("bitrate", ["2M", "750k", "1", "9999999", "500K", "3m"])
def test_a_bitrate_that_is_one_is_taken(bitrate: str):
    assert SessionParams(bitrate=bitrate).bitrate == bitrate


@pytest.mark.parametrize(
    "bitrate", ["2M -f lavfi", "$(id)", "2M;rm -rf /", "-i", "", "2 M", "0", "M"]
)
def test_a_bitrate_that_is_not_one_never_reaches_the_recorder(bitrate: str):
    with pytest.raises(ValidationError):
        SessionParams(bitrate=bitrate)


def test_a_parameter_nobody_declared_is_refused_rather_than_ignored():
    with pytest.raises(ValidationError):
        SessionParams.model_validate({"quality": "high"})


@pytest.mark.parametrize("name", ["work", "a.b-c_d", "A1", "x" * 64])
def test_a_profile_name_that_is_a_filename_is_taken(name: str):
    assert SessionCreate(profile=name).profile == name


@pytest.mark.parametrize(
    "name", ["../escape", "with space", ".hidden", "", "x" * 65, "a/b"]
)
def test_a_profile_name_that_could_be_a_path_is_refused(name: str):
    with pytest.raises(ValidationError):
        SessionCreate(profile=name)


def test_a_session_may_ask_for_no_profile_at_all():
    assert SessionCreate().profile is None


@pytest.mark.parametrize("limit", [0, -1])
def test_a_session_limit_below_one_is_refused(limit: int):
    with pytest.raises(ValidationError):
        SessionCreate(max_sessions=limit)


def test_a_pending_or_active_session_is_live_and_nothing_else_is():
    assert SessionStatus.PENDING.live
    assert SessionStatus.ACTIVE.live
    assert not SessionStatus.CLOSED.live
    assert not SessionStatus.DEAD.live


def test_a_command_is_read_back_as_the_kind_of_command_it_says_it_is():
    request = _request(method="goto", url="https://example.com/")

    assert isinstance(request.args, Goto)
    assert request.args.wait_until is WaitUntil.LOAD


def test_a_command_with_the_wrong_shape_for_its_method_is_refused():
    with pytest.raises(ValidationError):
        _request(method="goto")
    with pytest.raises(ValidationError):
        _request(method="type", selector="#name")
    with pytest.raises(ValidationError):
        _request(method="scroll_by", dy="a lot")


def test_a_method_this_build_never_heard_of_is_refused():
    with pytest.raises(ValidationError):
        _request(method="teleport", where="away")


@pytest.mark.parametrize("timeout", [0, -1, MAX_TIMEOUT + 1])
def test_a_command_timeout_that_would_not_survive_the_database_is_refused(
    timeout: float,
):
    with pytest.raises(ValidationError):
        CommandRequest(args=Bare(method=Method.TITLE), timeout=timeout)


def test_a_command_may_leave_the_timeout_to_the_session():
    assert CommandRequest(args=Bare(method=Method.TITLE)).timeout is None


@pytest.mark.parametrize(
    "args",
    [
        {"method": "back"},
        {"method": "click", "selector": "#save"},
        {"method": "activate", "index": 0},
        {"method": "eval", "expression": "1 + 1"},
        {"method": "goto", "url": "https://example.com/"},
        {"method": "new_tab"},
        {"method": "type", "selector": "#name", "text": "Ada"},
        {"method": "press", "key": "enter"},
        {"method": "hotkey", "keys": ["ctrl", "a"]},
        {"method": "select", "selector": "#colour", "value": "blue"},
        {"method": "scroll_by", "dy": -120},
        {"method": "upload", "selector": "#pick"},
        {"method": "html"},
        {"method": "attr", "selector": "#name", "name": "id"},
        {"method": "wait_for", "selector": "#late"},
        {"method": "wait_for_url", "pattern": "/done$"},
        {"method": "wait_for_load"},
        {"method": "init_script", "source": "window.x = 1"},
        {"method": "subscribe", "topics": ["tabs"]},
    ],
)
def test_every_kind_of_command_survives_the_wire(args: dict[str, object]):
    request = _request(**args)

    again = CommandRequest.model_validate_json(request.model_dump_json())

    assert again == request
    assert again.args.method == args["method"]


def test_an_envelope_carries_the_trace_it_was_enqueued_under():
    envelope = CommandEnvelope(
        command_id=uuid4(),
        seq=3,
        args=TypeText(selector="#name", text="Ada"),
        timeout_ms=30_000,
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )

    again = CommandEnvelope.model_validate_json(envelope.model_dump_json())

    assert again == envelope
    assert again.tracestate is None


def test_an_envelope_from_a_server_that_knows_no_trace_still_reads():
    envelope = CommandEnvelope.model_validate(
        {
            "command_id": str(uuid4()),
            "seq": 1,
            "args": {"method": "scroll_by", "dy": 10},
            "timeout_ms": 1000,
        }
    )

    assert isinstance(envelope.args, ScrollBy)
    assert envelope.traceparent is None


@pytest.mark.parametrize(
    "data",
    [
        {"type": "session_ready", "state_stale": True},
        {"type": "session_closed", "reason": "dead"},
        {"type": "command_started", "command_id": COMMAND_ID},
        {"type": "tab_opened", "index": 1, "url": "https://x/", "active": True},
        {"type": "tab_closed", "index": 1},
        {"type": "tab_activated", "index": 0},
        {"type": "download", "name": "a.bin", "size": 4, "url": "https://x/a.bin"},
    ],
)
def test_every_kind_of_event_survives_the_wire(data: dict[str, object]):
    event = Event.model_validate({"seq": 4, "data": data})

    again = Event.model_validate_json(event.model_dump_json())

    assert again == event
    assert again.data.type == data["type"]


def test_an_event_this_build_has_no_model_for_is_refused_rather_than_guessed():
    with pytest.raises(ValidationError):
        Event.model_validate({"seq": 1, "data": {"type": "teleported"}})


def test_an_event_names_its_own_type_without_being_told():
    announced = TabOpened(index=1, url="https://example.com/", active=True)

    assert announced.type is EventType.TAB_OPENED
    assert json.loads(announced.model_dump_json())["type"] == "tab_opened"


def test_the_union_of_events_covers_every_type_the_protocol_names():
    covered = {
        member.model_fields["type"].default
        for member in get_args(get_args(EventData)[0])
    }

    assert covered == set(EventType)
