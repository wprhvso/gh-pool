import logging

import uvicorn

from gh_pool.protocol import trace
from gh_pool.server.config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=trace.LOG_FORMAT)
    trace.install_logging()
    if not settings.token:
        raise SystemExit("GH_CHROME_TOKEN is not set")
    uvicorn.run(
        "pool.server.app:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
