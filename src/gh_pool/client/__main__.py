import argparse
import asyncio
import json

from gh_pool.client import profiles


def main() -> None:
    parser = argparse.ArgumentParser(prog="gh-pool")
    parser.add_argument("command", choices=["profiles"])
    parser.add_argument("--server", default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    found = asyncio.run(profiles(args.server, args.token))
    print(json.dumps([item.model_dump(mode="json") for item in found], indent=2))


if __name__ == "__main__":
    main()
