import asyncio

import gh_pool.client


async def main() -> None:
    session = await pool.client.new(width=1280, height=800, fps=10)
    print(f"session {session.id}, player at {session.player_url}")
    async with session as s:
        await s.ready(timeout=300)
        await s.goto("https://example.com")
        print(await s.title())
        await s.click("a")
        await s.wait_for_load()
        print(await s.url())


if __name__ == "__main__":
    asyncio.run(main())
