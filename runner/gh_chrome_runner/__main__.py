from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from uuid import UUID

from gh_chrome_runner.config import settings
from gh_chrome_runner.loop import Runner


def main() -> None:
    parser = argparse.ArgumentParser(prog="gh-chrome-runner")
    parser.add_argument("--session", required=True, type=UUID)
    parser.add_argument("--server", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.server:
        settings.url = args.server
    if not settings.token:
        raise SystemExit("GH_CHROME_TOKEN is not set")
    sys.exit(asyncio.run(Runner(args.session).run()))


if __name__ == "__main__":
    main()
