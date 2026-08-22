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
_MAX_TRACESTATE: Final = 512


def _hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in _HEX for char in value)


def _significant(value: str) -> bool:
    return value.strip("0") != ""


@dataclass(frozen=True, slots=True)
class TraceContext:
    traceparent: str
    tracestate: str | None = None

    @classmethod
    def parse(
        cls, traceparent: str | None, tracestate: str | None = None
    ) -> Self | None:
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
        if version == "00" and len(fields) != _FIELDS:
            return None
        return cls(traceparent=candidate, tracestate=_state(tracestate))

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Self | None:
        return cls.parse(headers.get(TRACEPARENT), headers.get(TRACESTATE))

    @property
    def trace_id(self) -> str:
        return self.traceparent.split("-")[1]

    @property
    def span_id(self) -> str:
        return self.traceparent.split("-")[2]

    def headers(self) -> dict[str, str]:
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
    context = TraceContext.parse(traceparent)
    return None if context is None else context.trace_id


NO_TRACE: Final = "-"

_current: ContextVar[TraceContext | None] = ContextVar("gh_chrome_trace", default=None)


def current() -> TraceContext | None:
    return _current.get()


@contextmanager
def bound(context: TraceContext | None) -> Generator[None]:
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = _current.get()
        record.trace_id = NO_TRACE if context is None else context.trace_id
        return True


def install_logging() -> None:
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, TraceIdFilter) for item in handler.filters):
            handler.addFilter(TraceIdFilter())


LOG_FORMAT: Final = "%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s"
