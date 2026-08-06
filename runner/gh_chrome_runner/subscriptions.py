from __future__ import annotations

import logging

from gh_chrome_protocol import Topic
from gh_chrome_protocol.events import TabActivated, TabClosed, TabOpened

from gh_chrome_runner.cdp import Cdp
from gh_chrome_runner.files import Files
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.tabs import Tabs

log = logging.getLogger(__name__)


class Subscriptions:
    def __init__(self, cdp: Cdp, server: ServerClient, tabs: Tabs, files: Files) -> None:
        self._cdp = cdp
        self._server = server
        self._tabs = tabs
        self._files = files
        self._active: set[Topic] = set()

    async def enable(self, topics: list[Topic]) -> None:
        wanted = set(topics)
        if Topic.TABS in wanted and Topic.TABS not in self._active:
            self._tabs.watch(self._opened, self._closed, self._activated)
        if Topic.TABS not in wanted and Topic.TABS in self._active:
            self._tabs.unwatch()
        if Topic.DOWNLOADS in wanted and Topic.DOWNLOADS not in self._active:
            self._files.watch()
        if Topic.DOWNLOADS not in wanted and Topic.DOWNLOADS in self._active:
            self._files.unwatch()
        self._active = wanted

    async def _opened(self, index: int, url: str, active: bool) -> None:
        await self._server.event(TabOpened(index=index, url=url, active=active))

    async def _closed(self, index: int) -> None:
        await self._server.event(TabClosed(index=index))

    async def _activated(self, index: int) -> None:
        await self._server.event(TabActivated(index=index))
