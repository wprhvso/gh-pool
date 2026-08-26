from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from gh_pool.core.obs import observability
from gh_pool.core.spans import record_command
from gh_pool.protocol import CommandError, ErrorCode


def test_without_a_collector_nothing_is_exported(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    config = observability("pool-worker", "1.2.3")

    assert not config.export_traces
    assert not config.export_logs
    assert not config.export_metrics


def test_with_a_collector_everything_is_exported(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")

    config = observability("pool-server", "1.2.3")

    assert config.export_traces
    assert config.export_logs
    assert config.export_metrics
    assert config.otlp_endpoint == "http://127.0.0.1:4317"


def test_the_service_name_and_version_are_carried_through(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    config = observability("pool-keeper", "9.9.9")

    assert config.service_name == "pool-keeper"
    assert config.service_version == "9.9.9"


def test_a_disabled_setup_does_not_advertise_a_collector(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    assert observability("pool-worker", "1.2.3").otlp_endpoint == ""


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
PARENT_SPAN_ID = 0x00F067AA0BA902B7


@pytest.fixture(scope="module")
def exported():
    memory = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return memory


def _row(**overrides):
    queued = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
    row = {
        "id": uuid4(),
        "seq": 7,
        "method": "goto",
        "queued_at": queued,
        "started_at": queued + timedelta(seconds=2),
        "finished_at": queued + timedelta(seconds=5),
        "traceparent": TRACEPARENT,
        "tracestate": None,
    }
    return row | overrides


def test_a_finished_command_lands_in_the_trace_that_asked_for_it(exported):
    exported.clear()

    record_command(uuid4(), _row(), None)

    (span,) = exported.get_finished_spans()
    assert span.name == "browser.command goto"
    assert span.context.trace_id == TRACE_ID
    assert span.parent.span_id == PARENT_SPAN_ID
    assert span.kind is SpanKind.SERVER


def test_the_span_covers_when_the_command_actually_ran(exported):
    exported.clear()

    record_command(uuid4(), _row(), None)

    (span,) = exported.get_finished_spans()
    # started_at to finished_at, not "whenever the server got round to it".
    assert span.end_time - span.start_time == 3 * 1_000_000_000


def test_waiting_for_a_runner_is_told_apart_from_running(exported):
    exported.clear()

    record_command(uuid4(), _row(), None)

    (span,) = exported.get_finished_spans()
    assert span.attributes["gh_pool.command.queued_ms"] == 2000
    assert span.attributes["gh_pool.command.method"] == "goto"
    assert span.attributes["gh_pool.command.seq"] == 7


def test_a_failed_command_is_a_failed_span(exported):
    exported.clear()

    record_command(
        uuid4(), _row(), CommandError(code=ErrorCode.TIMEOUT, message="too slow")
    )

    (span,) = exported.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["gh_pool.command.error.code"] == str(ErrorCode.TIMEOUT)


def test_a_command_nobody_traced_does_not_invent_a_trace(exported):
    exported.clear()

    record_command(uuid4(), _row(traceparent=None), None)

    assert exported.get_finished_spans() == ()
