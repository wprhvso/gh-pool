from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import httpx
from gh_chrome_protocol.commands import UploadArgs

from gh_chrome_runner.cdp import Cdp
from gh_chrome_runner.config import settings
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.locate import ElementMissing
from gh_chrome_runner.tabs import Tabs

log = logging.getLogger(__name__)

SETTLE = 0.3


class Files:
    def __init__(self, cdp: Cdp, server: ServerClient, tabs: Tabs) -> None:
        self._cdp = cdp
        self._server = server
        self._tabs = tabs
        self._names: dict[str, str] = {}
        self._watching = False

    async def upload(self, args: UploadArgs) -> None:
        path = await self._materialize(args)
        node = await self._node_id(args.selector)
        await self._cdp.send(
            "DOM.setFileInputFiles",
            {"files": [str(path)], "nodeId": node},
            self._tabs.active.session_id,
        )

    async def _materialize(self, args: UploadArgs) -> Path:
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        if args.file_id is not None:
            return await self._server.get_upload(args.file_id, settings.uploads_dir / args.file_id)
        if args.url is None:
            raise ValueError("upload needs either file_id or url")
        name = Path(httpx.URL(args.url).path).name or "upload.bin"
        target = settings.uploads_dir / name
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
            async with client.stream("GET", args.url) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
        return target

    async def _node_id(self, selector: str) -> int:
        session = self._tabs.active.session_id
        await self._cdp.send("DOM.enable", session_id=session)
        document = await self._cdp.send("DOM.getDocument", {"depth": 0}, session)
        result = await self._cdp.send(
            "DOM.querySelector",
            {"nodeId": document["root"]["nodeId"], "selector": selector},
            session,
        )
        node_id = int(result.get("nodeId", 0))
        if node_id == 0:
            raise ElementMissing(selector)
        return node_id

    def watch(self) -> None:
        if self._watching:
            return
        self._watching = True
        self._cdp.on("Browser.downloadWillBegin", self._will_begin)
        self._cdp.on("Browser.downloadProgress", self._progress)

    def unwatch(self) -> None:
        self._watching = False
        self._cdp.off("Browser.downloadWillBegin")
        self._cdp.off("Browser.downloadProgress")

    def _will_begin(self, message: dict[str, Any]) -> None:
        params = message["params"]
        self._names[params["guid"]] = params.get("suggestedFilename") or params["guid"]

    def _progress(self, message: dict[str, Any]) -> None:
        params = message["params"]
        if params.get("state") != "completed":
            return
        guid = params["guid"]
        name = self._names.pop(guid, guid)
        asyncio.get_running_loop().create_task(self._ship(guid, name))

    async def _ship(self, guid: str, name: str) -> None:
        source = settings.downloads_dir / guid
        await asyncio.sleep(SETTLE)
        if not source.exists():
            log.warning("download %s is missing on disk", guid)
            return
        with contextlib.suppress(Exception):
            await self._server.put_file(f"downloads/{name}", source)
            log.info("uploaded download %s", name)

    async def sweep(self) -> None:
        for path in settings.downloads_dir.glob("*"):
            if path.is_file() and not path.name.endswith(".crdownload"):
                with contextlib.suppress(Exception):
                    await self._server.put_file(f"downloads/{path.name}", path)
