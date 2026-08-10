import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import httpx

from gh_chrome_protocol import Upload
from gh_chrome_runner.cdp import Cdp
from gh_chrome_runner.config import settings
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.locate import ElementMissing
from gh_chrome_runner.tabs import Tabs

log = logging.getLogger(__name__)

SETTLE = 0.3


class Files:
    """Uploads into file inputs, and finished downloads back to the server."""

    def __init__(self, cdp: Cdp, server: ServerClient, tabs: Tabs) -> None:
        self._cdp = cdp
        self._server = server
        self._tabs = tabs
        self._names: dict[str, str] = {}
        self._shipping: set[asyncio.Task[None]] = set()
        self._watching = False

    async def upload(self, args: Upload) -> None:
        path = await self._materialize(args)
        node_id = await self._node_id(args.selector)
        await self._cdp.send(
            "DOM.setFileInputFiles",
            {"files": [str(path)], "nodeId": node_id},
            self._tabs.active.session_id,
        )

    async def _materialize(self, args: Upload) -> Path:
        """Put the file on the runner's disk, from the server or from a URL."""
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        if args.file_id is not None:
            return await self._server.get_upload(args.file_id, settings.uploads_dir)
        if args.url is None:
            raise ValueError("upload needs either file_id or url")
        target = settings.uploads_dir / (
            Path(httpx.URL(args.url).path).name or "upload.bin"
        )
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
        task = asyncio.get_running_loop().create_task(
            self._ship(guid, self._names.pop(guid, guid))
        )
        self._shipping.add(task)
        task.add_done_callback(self._shipping.discard)

    async def _ship(self, guid: str, name: str) -> None:
        await asyncio.sleep(SETTLE)
        source = settings.downloads_dir / guid
        if not source.exists():
            log.warning("download %s is missing on disk", guid)
            return
        with contextlib.suppress(Exception):
            await self._server.put_file(f"downloads/{name}", source)
            log.info("uploaded download %s", name)
