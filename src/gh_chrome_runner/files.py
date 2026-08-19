import asyncio
import contextlib
import ipaddress
import logging
import socket
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from gh_chrome_protocol import Upload
from gh_chrome_runner.cdp import Cdp
from gh_chrome_runner.config import settings
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.locate import ElementMissing
from gh_chrome_runner.tabs import Tabs

log = logging.getLogger(__name__)

SETTLE = 0.3
MAX_REDIRECTS = 5
SCHEMES = frozenset({"http", "https"})


async def _reachable(url: httpx.URL) -> None:
    if url.scheme not in SCHEMES:
        raise ValueError(f"upload will not fetch a {url.scheme or 'schemeless'} url")
    if settings.upload_allow_private:
        return
    host = url.host
    try:
        found = await asyncio.to_thread(
            socket.getaddrinfo, host, url.port or (443 if url.scheme == "https" else 80)
        )
    except OSError as exc:
        raise ValueError(f"{host} does not resolve") from exc
    for entry in found:
        address = ipaddress.ip_address(str(entry[4][0]))
        if not address.is_global:
            raise ValueError(
                f"upload will not fetch {host}: {address} is not a public address"
            )


def _one_segment(name: str | None, fallback: str) -> str:
    cleaned = PurePosixPath(name or "").name
    return fallback if cleaned in {"", ".", ".."} else cleaned


class Files:
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
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        if args.file_id is not None:
            return await self._server.get_upload(
                str(args.file_id), settings.uploads_dir
            )
        if args.url is None:
            raise ValueError("upload needs either file_id or url")
        target = settings.uploads_dir / _one_segment(
            Path(httpx.URL(args.url).path).name, "upload.bin"
        )
        url = httpx.URL(args.url)
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS):
                await _reachable(url)
                async with client.stream("GET", url) as response:
                    if response.is_redirect and response.has_redirect_location:
                        url = (
                            response.next_request.url if response.next_request else url
                        )
                        continue
                    response.raise_for_status()
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            handle.write(chunk)
                    return target
        raise ValueError(f"{args.url} redirects further than {MAX_REDIRECTS} hops")

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

    async def settle(self, timeout: float = 30.0) -> None:
        if not self._shipping:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await asyncio.gather(*self._shipping, return_exceptions=True)

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
        guid = params["guid"]
        self._names[guid] = _one_segment(params.get("suggestedFilename"), guid)

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
            await self._server.put_file(f"downloads/{quote(name, safe='')}", source)
            log.info("uploaded download %s", name)
