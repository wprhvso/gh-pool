import logging

import pytest

from gh_pool.protocol.trace import (
    NO_TRACE,
    TraceContext,
    TraceIdFilter,
    bound,
    current,
    trace_id_of,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
PARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


def test_a_well_formed_header_is_taken_apart() -> None:
    context = TraceContext.parse(PARENT)

    assert context is not None
    assert context.trace_id == TRACE_ID
    assert context.span_id == SPAN_ID
    assert context.traceparent == PARENT


@pytest.mark.parametrize(
    "traceparent",
    [
        None,
        "",
        "nonsense",
        f"00-{TRACE_ID}-{SPAN_ID}",
        f"ff-{TRACE_ID}-{SPAN_ID}-01",
        f"00-{'0' * 32}-{SPAN_ID}-01",
        f"00-{TRACE_ID}-{'0' * 16}-01",
        f"00-{TRACE_ID[:31]}-{SPAN_ID}-01",
        f"00-{'g' * 32}-{SPAN_ID}-01",
        f"00-{TRACE_ID}-{SPAN_ID}-0",
        f"00-{TRACE_ID}-{SPAN_ID}-01-extra",
    ],
)
def test_a_header_that_says_nothing_usable_is_refused(traceparent: str | None) -> None:
    assert TraceContext.parse(traceparent) is None


def test_a_version_from_the_future_is_still_carried() -> None:
    ahead = f"01-{TRACE_ID}-{SPAN_ID}-01-something-new"
    context = TraceContext.parse(ahead)

    assert context is not None
    assert context.traceparent == ahead
    assert context.trace_id == TRACE_ID


def test_the_headers_are_read_case_insensitively_as_http_allows() -> None:
    assert TraceContext.from_headers({"traceparent": PARENT}) is not None
    assert TraceContext.from_headers({}) is None


def test_an_oversized_tracestate_is_dropped_but_the_parent_is_kept() -> None:
    context = TraceContext.parse(PARENT, "v=" + "x" * 512)

    assert context is not None
    assert context.tracestate is None
    assert context.traceparent == PARENT


def test_the_carrier_round_trips() -> None:
    context = TraceContext.parse(PARENT, "v=1")

    assert context is not None
    assert context.headers() == {"traceparent": PARENT, "tracestate": "v=1"}
    assert TraceContext.from_headers(context.headers()) == context


def test_trace_id_of_reads_straight_through() -> None:
    assert trace_id_of(PARENT) == TRACE_ID
    assert trace_id_of("nonsense") is None
    assert trace_id_of(None) is None


def test_binding_a_context_is_undone_on_the_way_out() -> None:
    assert current() is None
    with bound(TraceContext.parse(PARENT)):
        held = current()
        assert held is not None
        assert held.trace_id == TRACE_ID
    assert current() is None


def _record() -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)


def test_a_record_made_inside_a_trace_carries_its_id() -> None:
    entry = _record()
    with bound(TraceContext.parse(PARENT)):
        assert TraceIdFilter().filter(entry)
    assert entry.trace_id == TRACE_ID  # pyright: ignore[reportAttributeAccessIssue]


def test_a_record_made_outside_one_still_has_the_field() -> None:
    entry = _record()

    assert TraceIdFilter().filter(entry)
    assert entry.trace_id == NO_TRACE  # pyright: ignore[reportAttributeAccessIssue]
