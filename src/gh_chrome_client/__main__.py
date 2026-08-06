from __future__ import annotations

import argparse
import asyncio
import json

from gh_chrome_client import profiles


def main() -> None:
    parser = argparse.ArgumentParser(prog="gh-chrome")
    parser.add_argument("--server", default=None)
    parser.add_argument("--token", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("profiles")
    args = parser.parse_args()

    if args.command == "profiles":
        found = asyncio.run(profiles(args.server, args.token))
        print(json.dumps([item.model_dump(mode="json") for item in found], indent=2))


if __name__ == "__main__":
    main()
