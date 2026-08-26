"""Spans for work that happens somewhere without an OpenTelemetry SDK.

The browser runner is installed with `uv run --with` at the start of every
session, so its dependency list is deliberately narrow -- narrow enough that
shipping an SDK there to emit one span per command is not worth what it costs
every session start. The server has everything the span needs anyway: the
caller's traceparent, stored on the row at enqueue, and the two timestamps
Postgres wrote around the run. So the span is emitted here, after the fact,
with explicit start and end times, and lands in the caller's trace exactly
where the work happened.

What this does not give you is the runner's own breakdown -- the CDP round
trips, the navigation, the waiting. That needs an SDK on the runner. This
closes the gap where the trace stopped at "accepted" and picked up again at
the answer with nothing in between.
"""

import logging
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from yaol import extract_context

from gh_pool.protocol import CommandError

log: Final = logging.getLogger(__name__)

_tracer: Final = trace.get_tracer("gh_pool.server")

_NANOS: Final = 1_000_000_000


def _nanos(moment: object) -> int | None:
    if not isinstance(moment, datetime):
        return None
    return int(moment.timestamp() * _NANOS)


def _carrier(row: dict[str, Any]) -> dict[str, str] | None:
    traceparent = row.get("traceparent")
    if not isinstance(traceparent, str) or not traceparent:
        return None
    carrier = {"traceparent": traceparent}
    tracestate = row.get("tracestate")
    if isinstance(tracestate, str) and tracestate:
        carrier["tracestate"] = tracestate
    return carrier


def record_command(
    session_id: UUID, row: dict[str, Any], error: CommandError | None
) -> None:
    """Put one finished command into the trace that asked for it."""
    # This runs on the completion path. A command that ran is finished whether
    # or not anyone managed to describe it, so nothing in here is allowed to be
    # the reason the runner is told otherwise.
    try:
        _record(session_id, row, error)
    except Exception:
        log.warning("could not record the span for command %s", row.get("id"))


def _record(session_id: UUID, row: dict[str, Any], error: CommandError | None) -> None:
    carrier = _carrier(row)
    if carrier is None:
        # Nothing asked for it in a trace -- a cancel, a watchdog sweep of a
        # command enqueued before propagation reached that caller. A root span
        # here would be a trace of one span that means nothing on its own.
        return

    started = _nanos(row.get("started_at"))
    finished = _nanos(row.get("finished_at"))

    attributes: dict[str, Any] = {
        "gh_pool.session.id": str(session_id),
        "gh_pool.command.id": str(row.get("id")),
        "gh_pool.command.method": str(row.get("method")),
    }
    seq = row.get("seq")
    if seq is not None:
        attributes["gh_pool.command.seq"] = int(seq)

    # Queue wait as an attribute rather than inside the span: a command that
    # waited ten seconds for a free runner and one that took ten seconds to run
    # are different problems, and a single duration cannot tell them apart.
    queued = _nanos(row.get("queued_at"))
    if queued is not None and started is not None:
        attributes["gh_pool.command.queued_ms"] = (started - queued) // 1_000_000

    span = _tracer.start_span(
        f"browser.command {row.get('method')}",
        context=extract_context(carrier),
        kind=SpanKind.SERVER,
        start_time=started,
        attributes=attributes,
    )
    if error is not None:
        span.set_attribute("gh_pool.command.error.code", str(error.code))
        span.set_status(Status(StatusCode.ERROR, str(error.code)))
    span.end(end_time=finished)
