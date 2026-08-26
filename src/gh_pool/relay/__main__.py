import logging

import uvicorn
from yaol import instrument_fastapi, instrument_runtime, setup, shutdown

from gh_pool.core.config import settings
from gh_pool.core.obs import observability, version
from gh_pool.protocol import trace
from gh_pool.relay.app import app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=trace.LOG_FORMAT)
    trace.install_logging()
    if not settings.token:
        raise SystemExit("GH_POOL_TOKEN is not set")
    setup(observability("pool-relay", version()))
    instrument_fastapi(app)
    # In a container /proc is the node's: system.* would report the whole
    # host once per pod, on top of what cAdvisor already says about it.
    instrument_runtime(system_metrics=False)
    try:
        uvicorn.run(
            app,
            host=settings.relay_host,
            port=settings.relay_port,
            log_config=None,
        )
    finally:
        shutdown()


if __name__ == "__main__":
    main()
