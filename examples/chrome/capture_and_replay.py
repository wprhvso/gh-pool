import asyncio

import pool.client
from pool.client import Rule, Tap

RULE = Rule(
    name="search",
    url="/complete/search",
    method="GET",
    action="capture",
    status=204,
)


async def main() -> None:
    session = await pool.client.new(width=1280, height=800, fps=10)
    print(f"session {session.id}, player at {session.player_url}")
    async with session as s:
        await s.ready(timeout=300)

        tap = Tap(s)
        await tap.arm()
        await s.goto("https://duckduckgo.com")
        await tap.install([RULE])

        await s.type("input[name=q]", "gh-chrome")
        captured = await tap.take("search", timeout=30)
        print(f"the page was about to call {captured.url}")

        async for chunk in tap.replay(captured, timeout=60):
            print(chunk, end="")
        print()


if __name__ == "__main__":
    asyncio.run(main())
