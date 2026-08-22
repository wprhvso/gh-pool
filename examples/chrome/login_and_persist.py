import asyncio
import os

import gh_chrome_client
from gh_chrome_client import Topic

USERNAME = os.environ["DEMO_USERNAME"]
PASSWORD = os.environ["DEMO_PASSWORD"]


async def main() -> None:
    session = await gh_chrome_client.new(
        profile="demo",
        subscribe=[Topic.TABS, Topic.DOWNLOADS],
        timeout=45.0,
    )
    async with session as s:
        await s.ready(timeout=300)
        if s.state_stale:
            print("warning: the previous runner died, the saved state may be outdated")

        await s.goto("https://github.com/login")

        if "login" not in await s.url():
            print("already signed in, the profile survived")
            return

        await s.type("#login_field", USERNAME)
        await s.type("#password", PASSWORD)
        await s.click('input[name="commit"]')

        try:
            await s.wait_for_url(r"github\.com/?$", timeout=60)
        except gh_chrome_client.CommandTimeout:
            print("two-factor or a captcha is in the way, watch the recording")
            return

        print("signed in as", await s.text(".AppHeader-user"))


if __name__ == "__main__":
    asyncio.run(main())
