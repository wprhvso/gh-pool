from pathlib import Path

import pytest

from gh_pool.browser.config import settings
from gh_pool.browser.display import Display, _kasmvnc_command, _xvfb_command


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode


def _display(*processes: FakeProcess) -> Display:
    display = Display(1280, 720)
    display._processes = list(processes)  # pyright: ignore[reportAttributeAccessIssue]
    return display


def test_a_desktop_is_asked_for_at_the_size_the_session_wanted():
    command = _kasmvnc_command(":99", 1600, 900)

    assert command[1] == ":99"
    assert command[command.index("-geometry") + 1] == "1600x900"
    assert command[command.index("-interface") + 1] == "127.0.0.1"


def test_the_desktop_never_asks_the_internet_where_it_lives():
    command = _kasmvnc_command(":99", 800, 600)

    assert command[command.index("-PublicIP") + 1] == "127.0.0.1"


def test_a_screen_with_no_desktop_is_asked_for_at_the_same_size():
    command = _xvfb_command(":99", 1600, 900)

    assert command[1] == ":99"
    assert "1600x900x24" in command
    assert "-nolisten" in command


def test_a_display_that_never_came_up_is_not_alive():
    assert not _display().alive()


def test_a_display_whose_server_is_running_is_alive():
    assert _display(FakeProcess(), FakeProcess()).alive()


def test_a_window_manager_that_crashed_does_not_end_the_session():
    assert _display(FakeProcess(), FakeProcess(returncode=1)).alive()


def test_a_screen_that_died_ends_the_session():
    assert not _display(FakeProcess(returncode=1), FakeProcess()).alive()


def test_a_display_is_named_by_the_number_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "display", 42)

    assert Display(800, 600).name == ":42"
    assert Display(800, 600).env["DISPLAY"] == ":42"


def test_a_session_without_the_desktop_asked_for_does_not_look_for_it(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "vnc", False)

    assert not Display(800, 600)._kasmvnc_ready()


def test_a_desktop_whose_client_is_missing_is_not_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(settings, "vnc", True)
    monkeypatch.setattr(settings, "kasmvnc_binary", "sh")
    monkeypatch.setattr(settings, "vnc_www", tmp_path / "nothing-here")

    assert not Display(800, 600)._kasmvnc_ready()
