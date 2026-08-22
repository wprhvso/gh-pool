from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from pool.keeper import __main__ as cli_mod
from pool.keeper.config import Server, Target
from pool.keeper.errors import RunnerError
from tests.keeper.fake import FakePool

CONFIG = """
label = "pool"

[pool]
server = "https://pool.example"
token = "клиент"

[repos]
"owner/app" = "ghp_a"
"""


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "_observe", lambda: None)
    monkeypatch.setattr(cli_mod, "shutdown", lambda: None)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "runners.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _healthy(monkeypatch: pytest.MonkeyPatch, pool: FakePool) -> None:
    monkeypatch.setattr(cli_mod, "Pool", lambda _server: pool)
    monkeypatch.setattr(cli_mod, "preflight", lambda _target: {"private": True})
    monkeypatch.setattr(cli_mod, "release_version", lambda: "2.999.0")


def test_a_check_of_a_healthy_setup_passes(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy(monkeypatch, FakePool(run=False))
    assert cli_mod.main(["--check", "-c", str(config)]) == 0


def test_a_check_fails_when_a_repo_is_out_of_reach(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy(monkeypatch, FakePool(run=False))

    def refuse(_target: Target) -> dict[str, Any]:
        raise RunnerError("нет доступа")

    monkeypatch.setattr(cli_mod, "preflight", refuse)

    assert cli_mod.main(["--check", "-c", str(config)]) == 1


def test_a_check_fails_when_the_pool_is_down(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FakePool(run=False)
    _healthy(monkeypatch, pool)

    def refuse() -> dict[str, Any]:
        raise RunnerError("пул лежит")

    monkeypatch.setattr(pool, "health", refuse)

    assert cli_mod.main(["--check", "-c", str(config)]) == 1


def test_a_config_and_repos_together_are_refused(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_mod.main(["owner/app", "-c", str(config)]) == 2
    assert "конфиг" in capsys.readouterr().err


def test_nothing_to_watch_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_mod.main([]) == 2
    assert capsys.readouterr().err


def test_a_config_gives_the_pool_and_the_repos(config: Path) -> None:
    targets, server = cli_mod._targets(
        cli_mod.argparse.Namespace(config=config, repos=[])
    )
    assert [target.slug for target in targets] == ["owner/app"]
    assert targets[0].token == "ghp_a"
    assert server == Server(url="https://pool.example", token="клиент")


def test_repos_from_the_command_line_use_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_env")
    monkeypatch.setenv("POOL_SERVER", "https://pool.env/")
    monkeypatch.setenv("RUNNERS_LABEL", "своя")

    targets, server = cli_mod._targets(
        cli_mod.argparse.Namespace(config=None, repos=["owner/app"])
    )

    assert targets[0].token == "ghp_env"
    assert targets[0].label == "своя"
    assert server.url == "https://pool.env"


def test_a_world_readable_config_is_flagged(
    config: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config.chmod(0o644)
    with caplog.at_level("WARNING"):
        cli_mod._targets(cli_mod.argparse.Namespace(config=config, repos=[]))
    assert "chmod 600" in caplog.text


def test_a_run_that_keeps_falling_is_retried_until_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "backoff", lambda *_a, **_kw: 0.0)
    attempts: list[int] = []
    stop = threading.Event()

    def falls(*_args: object) -> int:
        attempts.append(1)
        if len(attempts) == 3:
            stop.set()
        raise RunnerError("не поднялось")

    monkeypatch.setattr(cli_mod, "run", falls)
    results: dict[str, int] = {}
    target = Target(slug="owner/app", token="ghp")

    cli_mod._worker(target, Server("https://pool", "t"), stop, results)

    assert len(attempts) == 4
    assert results["owner/app"] == 1


def test_a_clean_run_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def once(*_args: object) -> int:
        attempts.append(1)
        return 0

    monkeypatch.setattr(cli_mod, "run", once)
    results: dict[str, int] = {}

    cli_mod._worker(
        Target(slug="owner/app", token="ghp"),
        Server("https://pool", "t"),
        threading.Event(),
        results,
    )

    assert attempts == [1]
    assert results == {"owner/app": 0}


def test_an_unexpected_crash_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def explodes(*_args: object) -> int:
        raise ZeroDivisionError

    monkeypatch.setattr(cli_mod, "run", explodes)
    results: dict[str, int] = {}

    assert not cli_mod._attempt(
        Target(slug="owner/app", token="ghp"),
        Server("https://pool", "t"),
        threading.Event(),
        results,
    )
    assert results["owner/app"] == 1
