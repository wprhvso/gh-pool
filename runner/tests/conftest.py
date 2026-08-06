from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from gh_chrome_protocol import SessionParams
from gh_chrome_runner.browser import Browser
from gh_chrome_runner.config import settings
from gh_chrome_runner.display import Display
from gh_chrome_runner.input import Input
from gh_chrome_runner.tabs import Tabs

PAGE = """
<!doctype html><meta charset="utf-8"><title>fixture</title>
<style>
  body { margin: 0; font: 16px sans-serif; }
  #tall { height: 3000px; }
  #btn { position: absolute; top: 400px; left: 200px; width: 160px; height: 48px; }
  #cover { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: none; }
  #moving { position: absolute; top: 900px; left: 0; animation: slide 1s ease-out forwards; }
  @keyframes slide { to { left: 300px; } }
</style>
<div id="tall">
  <button id="btn" onclick="this.dataset.clicked='yes'">click me</button>
  <input id="field" style="position:absolute;top:600px;left:200px;width:300px">
  <button id="moving">moving</button>
  <div id="cover"></div>
</div>
"""


def requires_display() -> None:
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb is not available")
    if os.environ.get("GH_CHROME_SKIP_X"):
        pytest.skip("display tests are disabled")


@pytest.fixture
async def workdir(tmp_path: Path) -> Path:
    settings.workdir = tmp_path
    return tmp_path


@pytest.fixture
async def display(workdir: Path) -> AsyncIterator[Display]:
    requires_display()
    instance = Display(1280, 800)
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
async def page(display: Display, workdir: Path) -> AsyncIterator[tuple[Tabs, Input]]:
    params = SessionParams(width=1280, height=800)
    browser = Browser(display, params)
    await browser.start()
    assert browser.cdp is not None
    tabs = Tabs(browser.cdp)
    await tabs.start()
    controls = Input(browser.cdp, display, tabs, params)
    await controls.start()
    path = workdir / "fixture.html"
    path.write_text(PAGE)
    await tabs.navigate(f"file://{path}")
    await asyncio.sleep(0.5)
    yield tabs, controls
    await browser.stop()
