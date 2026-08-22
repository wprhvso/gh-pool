import argparse
import asyncio
import logging
import sys
from uuid import UUID

from gh_pool.browser.config import settings
from gh_pool.browser.loop import Runner
from gh_pool.protocol import trace


def main() -> None:
    parser = argparse.ArgumentParser(prog="gh-pool-browser")
    parser.add_argument("--session", required=True, type=UUID)
    parser.add_argument("--server", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=trace.LOG_FORMAT,
        stream=sys.stderr,
    )
    trace.install_logging()
    if args.server:
        settings.url = args.server
    if not settings.token:
        raise SystemExit("GH_POOL_TOKEN is not set")
    sys.exit(asyncio.run(Runner(args.session).run()))


if __name__ == "__main__":
    main()
