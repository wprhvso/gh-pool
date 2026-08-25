from gh_pool.core.obs import observability


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
