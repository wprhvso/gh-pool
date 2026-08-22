from pathlib import Path

import pytest

from gh_chrome_runner.config import Settings, settings


def test_every_directory_the_runner_uses_is_under_its_own_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "workdir", tmp_path)

    for directory in (
        settings.profile_dir,
        settings.downloads_dir,
        settings.segments_dir,
        settings.uploads_dir,
        settings.logs_dir,
    ):
        assert directory.parent == tmp_path


def test_the_browser_the_operator_named_is_the_one_that_is_run():
    chosen = Settings(chrome_binary="/opt/chrome/chrome")

    assert chosen.chrome == "/opt/chrome/chrome"


def test_without_a_choice_a_browser_is_looked_for_by_name():
    assert Settings(chrome_binary="").chrome


def test_the_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_CHROME_URL", "https://chrome.example.com")
    monkeypatch.setenv("GH_CHROME_TOKEN", "a-secret")
    monkeypatch.setenv("GH_CHROME_DISPLAY", "77")

    read = Settings()

    assert read.url == "https://chrome.example.com"
    assert read.token == "a-secret"
    assert read.display_name == ":77"


def test_a_setting_nobody_declared_is_ignored_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GH_CHROME_SOMETHING_ELSE", "1")

    assert Settings().display == 99
