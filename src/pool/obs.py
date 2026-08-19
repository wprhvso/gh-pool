import os
from dataclasses import replace

from yaol import ObservabilityConfig, from_env


def observability(service: str, service_version: str) -> ObservabilityConfig:
    config = from_env(
        service,
        service_version=service_version,
        environment=os.getenv("ENV", "prod"),
    )
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return config
    return replace(
        config,
        otlp_endpoint="",
        export_traces=False,
        export_logs=False,
        export_metrics=False,
    )
