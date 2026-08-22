from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from tests.e2e.stack import Background

from gh_chrome_protocol import Upload
from gh_chrome_runner.config import settings
from gh_chrome_runner.files import Files, _one_segment, _reachable

PAYLOAD = b"a file the page asked for" * 20


class FakeCdp:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def off(self, event: str, _handler: Any = None) -> None:
        self.listeners.pop(event, None)

    def emit(self, method: str, params: dict[str, Any]) -> None:
        for handler in list(self.listeners.get(method, ())):
            handler({"method": method, "params": params})


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "workdir", tmp_path)
    monkeypatch.setattr(settings, "upload_allow_private", True)
    return tmp_path


@pytest.fixture
def site() -> Iterator[Background]:
    async def asset(_request: Request) -> Response:
        return Response(PAYLOAD, media_type="application/octet-stream")

    async def once(_request: Request) -> Response:
        return RedirectResponse("/asset/report.bin")

    async def forever(request: Request) -> Response:
        step = int(request.path_params["step"])
        return RedirectResponse(f"/forever/{step + 1}")

    async def missing(_request: Request) -> Response:
        return Response(status_code=404)

    running = Background(
        Starlette(
            routes=[
                Route("/", asset),
                Route("/asset/{name}", asset),
                Route("/once", once),
                Route("/forever/{step}", forever),
                Route("/missing", missing),
            ]
        )
    )
    running.start()
    try:
        yield running
    finally:
        running.stop()


def _files(cdp: FakeCdp | None = None) -> Files:
    return Files(cdp or FakeCdp(), None, None)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("suggested", "kept"),
    [
        ("report.bin", "report.bin"),
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("../profile", "profile"),
        ("..", "the-guid"),
        (".", "the-guid"),
        ("", "the-guid"),
        (None, "the-guid"),
        ("a report #1.bin", "a report #1.bin"),
    ],
)
def test_a_download_is_kept_under_a_name_that_is_only_a_name(
    suggested: str | None, kept: str
):
    assert _one_segment(suggested, "the-guid") == kept


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "/just/a/path",
        "data:text/plain,hello",
    ],
)
async def test_an_upload_will_only_be_fetched_over_http(url: str):
    with pytest.raises(ValueError, match="will not fetch"):
        await _reachable(httpx.URL(url))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/secret",
        "http://localhost/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/inside",
        "http://[::1]:9000/secret",
    ],
)
async def test_an_upload_will_not_be_fetched_from_the_network_around_the_runner(
    url: str,
):
    with pytest.raises(ValueError, match="not a public address"):
        await _reachable(httpx.URL(url))


async def test_a_host_that_does_not_resolve_is_refused():
    with pytest.raises(ValueError, match="does not resolve"):
        await _reachable(httpx.URL("http://nothing.here.invalid/x"))


async def test_a_runner_told_to_trust_its_own_network_may_fetch_from_it(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "upload_allow_private", True)

    await _reachable(httpx.URL("http://127.0.0.1:8000/allowed"))


async def test_a_file_the_page_asked_for_is_fetched_to_the_uploads_directory(
    workdir: Path, site: Background
):
    args = Upload(selector="#pick", url=f"{site.url}/asset/report.bin")

    fetched = await _files()._materialize(args)

    assert fetched.read_bytes() == PAYLOAD
    assert fetched.name == "report.bin"
    assert fetched.parent == settings.uploads_dir


async def test_a_url_with_no_filename_still_lands_somewhere(
    workdir: Path, site: Background
):
    args = Upload(selector="#pick", url=f"{site.url}/")

    fetched = await _files()._materialize(args)

    assert fetched.name == "upload.bin"


async def test_a_redirect_is_followed(workdir: Path, site: Background):
    args = Upload(selector="#pick", url=f"{site.url}/once")

    fetched = await _files()._materialize(args)

    assert fetched.read_bytes() == PAYLOAD


async def test_a_redirect_that_never_ends_is_given_up_on(
    workdir: Path, site: Background
):
    args = Upload(selector="#pick", url=f"{site.url}/forever/0")

    with pytest.raises(ValueError, match="redirects further"):
        await _files()._materialize(args)


async def test_a_file_that_is_not_there_is_reported(workdir: Path, site: Background):
    args = Upload(selector="#pick", url=f"{site.url}/missing")

    with pytest.raises(httpx.HTTPStatusError):
        await _files()._materialize(args)


async def test_an_upload_needs_something_to_fetch(workdir: Path):
    with pytest.raises(ValueError, match="either file_id or url"):
        await _files()._materialize(Upload(selector="#pick"))


def test_a_session_that_asked_for_downloads_is_listening():
    cdp = FakeCdp()
    files = _files(cdp)

    files.watch()

    assert "Browser.downloadWillBegin" in cdp.listeners
    assert "Browser.downloadProgress" in cdp.listeners


def test_a_session_that_stopped_asking_is_not(workdir: Path):
    cdp = FakeCdp()
    files = _files(cdp)
    files.watch()

    files.unwatch()

    assert cdp.listeners.get("Browser.downloadWillBegin") is None


def test_the_name_a_page_chose_is_reduced_to_a_filename(workdir: Path):
    cdp = FakeCdp()
    files = _files(cdp)
    files.watch()

    cdp.emit(
        "Browser.downloadWillBegin",
        {"guid": "g-1", "suggestedFilename": "../../somewhere/else.bin"},
    )

    assert files._names["g-1"] == "else.bin"


def test_a_download_the_page_did_not_name_is_known_by_its_own_id(workdir: Path):
    cdp = FakeCdp()
    files = _files(cdp)
    files.watch()

    cdp.emit("Browser.downloadWillBegin", {"guid": "g-2"})

    assert files._names["g-2"] == "g-2"


async def test_a_settle_with_nothing_in_flight_returns_at_once(workdir: Path):
    await _files().settle(timeout=0.1)


def test_a_download_that_is_still_going_is_not_shipped(workdir: Path):
    cdp = FakeCdp()
    files = _files(cdp)
    files.watch()

    cdp.emit("Browser.downloadProgress", {"guid": "g-3", "state": "inProgress"})

    assert files._shipping == set()
