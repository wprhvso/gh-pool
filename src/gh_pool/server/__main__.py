import logging

import uvicorn
from yaol import instrument_fastapi, instrument_runtime, instrument_sqlalchemy, setup, shutdown

from gh_pool.core.config import settings
from gh_pool.db.engine import engine
from gh_pool.obs import observability, version
from gh_pool.protocol import trace
from gh_pool.server.app import app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=trace.LOG_FORMAT)
    trace.install_logging()
    if not settings.token:
        raise SystemExit("GH_POOL_TOKEN is not set")
    setup(observability("pool-server", version()))
    instrument_fastapi(app)
    instrument_sqlalchemy(engine())
    instrument_runtime()
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_config=None,
        )
    finally:
        shutdown()


if __name__ == "__main__":
    main()
