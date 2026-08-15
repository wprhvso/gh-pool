"""W3C trace context, carried to the runner through the command queue.

Whoever drives this client traces its own HTTP hop — the gateway that runs the
browser providers instruments httpx — so a ``traceparent`` header arrives on
the request that enqueues a command. The runner that eventually executes that
command sits on the far end of a different connection, opened long before and
belonging to no request, so the header cannot reach it on its own: the context
travels with the command instead, and the runner picks the trace back up from
there.

Nothing here depends on OpenTelemetry. A header is parsed only far enough to
refuse one that is malformed, kept as the string it arrived as, and handed back
out unchanged — a version this code has never heard of is still forwarded,
which is what the specification asks for.
"""

import logging
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final, Self

TRACEPARENT: Final = "traceparent"
TRACESTATE: Final = "tracestate"

_HEX: Final = frozenset("0123456789abcdef")
_VERSION_LENGTH: Final = 2
_TRACE_ID_LENGTH: Final = 32
_SPAN_ID_LENGTH: Final = 16
_FLAGS_LENGTH: Final = 2
_FIELDS: Final = 4
_INVALID_VERSION: Final = "ff"
# The specification's own ceiling for what a receiver has to carry.
_MAX_TRACESTATE: Final = 512


def _hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in _HEX for char in value)


def _significant(value: str) -> bool:
    """An id of nothing but zeros is the specification's way of saying absent."""
    return value.strip("0") != ""


@dataclass(frozen=True, slots=True)
class TraceContext:
    """A validated ``traceparent``, and the ``tracestate`` that came with it."""

    traceparent: str
    tracestate: str | None = None

    @classmethod
    def parse(
        cls, traceparent: str | None, tracestate: str | None = None
    ) -> Self | None:
        """The context these headers describe, or None if they describe nothing."""
        if traceparent is None:
            return None
        candidate = traceparent.strip()
        fields = candidate.split("-")
        if len(fields) < _FIELDS:
            return None
        version, trace_id, span_id, flags = fields[:_FIELDS]
        if not _hex(version, _VERSION_LENGTH) or version == _INVALID_VERSION:
            return None
        if not _hex(trace_id, _TRACE_ID_LENGTH) or not _significant(trace_id):
            return None
        if not _hex(span_id, _SPAN_ID_LENGTH) or not _significant(span_id):
            return None
        if not _hex(flags, _FLAGS_LENGTH):
            return None
        # Version 00 is exactly four fields; anything later may carry more, and
        # forwarding what we cannot read is the point of the version prefix.
        if version == "00" and len(fields) != _FIELDS:
            return None
        return cls(traceparent=candidate, tracestate=_state(tracestate))

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Self | None:
        """The context an incoming request carries, if it carries one."""
        return cls.parse(headers.get(TRACEPARENT), headers.get(TRACESTATE))

    @property
    def trace_id(self) -> str:
        """The id that identifies the whole trace, for logs to quote."""
        return self.traceparent.split("-")[1]

    @property
    def span_id(self) -> str:
        """The id of the span this context points at — the runner's parent."""
        return self.traceparent.split("-")[2]

    def headers(self) -> dict[str, str]:
        """The carrier to hand to anything that speaks W3C trace context."""
        carrier = {TRACEPARENT: self.traceparent}
        if self.tracestate is not None:
            carrier[TRACESTATE] = self.tracestate
        return carrier


def _state(tracestate: str | None) -> str | None:
    if tracestate is None:
        return None
    candidate = tracestate.strip()
    if not candidate or len(candidate) > _MAX_TRACESTATE:
        return None
    return candidate


def trace_id_of(traceparent: str | None) -> str | None:
    """The trace id inside a header, when there is a usable one."""
    context = TraceContext.parse(traceparent)
    return None if context is None else context.trace_id


NO_TRACE: Final = "-"

_current: ContextVar[TraceContext | None] = ContextVar("gh_chrome_trace", default=None)


def current() -> TraceContext | None:
    """The trace this task is working on behalf of, if it knows."""
    return _current.get()


@contextmanager
def bound(context: TraceContext | None) -> Generator[None]:
    """Work done in this block belongs to the caller's trace."""
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


class TraceIdFilter(logging.Filter):
    """Stamps every record with the trace it belongs to.

    Nothing here exports a span, so the trace id in the log is the whole of the
    correlation: it is what lets a request followed through the gateway be
    picked back up in the server's log and again in the runner's, which are
    three processes and two machines apart.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _current.get()
        record.trace_id = NO_TRACE if context is None else context.trace_id
        return True


def install_logging() -> None:
    """Put the trace id on the records of every logger, not just ours.

    The filter goes on the root handlers rather than on a logger, so a record
    from uvicorn or httpx is stamped too and the format string below can count
    on the field being there.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, TraceIdFilter) for item in handler.filters):
            handler.addFilter(TraceIdFilter())


LOG_FORMAT: Final = "%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s"
