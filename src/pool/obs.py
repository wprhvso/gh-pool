import os
from dataclasses import replace

from yaol import ObservabilityConfig, from_env


def observability(service: str, service_version: str) -> ObservabilityConfig:
    """Настройки наблюдаемости, честные к отсутствию коллектора.

    from_env по умолчанию целится в localhost:4317. Воркер живёт на эфемерном
    GitHub-раннере, где этот адрес никто не слушает, и экспортёр раз в секунду
    печатал отказ связи прямо в вывод шага. Нет адреса — нет экспорта; логи при
    этом никуда не деваются, они идут в stderr.
    """
    config = from_env(
        service,
        service_version=service_version,
        environment=os.getenv("ENV", "prod"),
    )
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return config
    # Адрес тоже гасим: иначе строка о запуске рапортует коллектор, которого нет
    # и в который никто не собирается писать.
    return replace(
        config,
        otlp_endpoint="",
        export_traces=False,
        export_logs=False,
        export_metrics=False,
    )
