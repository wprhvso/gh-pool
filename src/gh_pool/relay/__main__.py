import logging

import uvicorn

from gh_pool.protocol import trace
from gh_pool.server.config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=trace.LOG_FORMAT)
    trace.install_logging()
    if not settings.token:
        raise SystemExit("GH_POOL_TOKEN is not set")
    uvicorn.run(
        "gh_pool.relay.app:app",
        host=settings.relay_host,
        port=settings.relay_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
