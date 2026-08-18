from __future__ import annotations

from pathlib import Path

import pytest
from pool_runners.config import Target, env_target, load, secs
from pool_runners.errors import RunnerError

CONFIG = """
jobs = 8
idle = "2m"
label = "gpu"

[pool]
server = "https://pool.example.com/"
token = "клиентский"

[repos]
"alice/app" = "ghp_alice"
"bob/service" = { token = "ghp_bob", jobs = 40, lifetime = "2h" }
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "runners.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_durations() -> None:
    assert secs("30s") == 30
    assert secs("5m") == 300
    assert secs("6h") == 21600
    assert secs(90) == 90
    with pytest.raises(RunnerError):
        secs("завтра")


def test_defaults_flow_down_to_every_repo(tmp_path: Path) -> None:
    targets, server = load(_write(tmp_path, CONFIG))
    alice, bob = sorted(targets, key=lambda t: t.slug)

    assert server.url == "https://pool.example.com"
    assert server.token == "клиентский"

    assert alice.token == "ghp_alice"
    assert alice.jobs == 8
    assert alice.idle == 120
    assert alice.label == "gpu"

    assert bob.jobs == 40
    assert bob.lifetime == 7200
    assert bob.idle == 120


def test_repo_without_token_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunnerError):
        load(_write(tmp_path, '[repos]\n"alice/app" = { jobs = 2 }\n'))


def test_empty_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunnerError):
        load(_write(tmp_path, "jobs = 3\n"))


def test_broken_toml_says_so(tmp_path: Path) -> None:
    with pytest.raises(RunnerError):
        load(_write(tmp_path, "это не toml ="))


def test_slug_shape_is_checked() -> None:
    with pytest.raises(RunnerError):
        Target(slug="просто-имя", token="x").check()
    with pytest.raises(RunnerError):
        Target(slug="owner/name", token="").check()


def test_env_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_env")
    monkeypatch.setenv("RUNNERS_LABEL", "своя")
    monkeypatch.setenv("RUNNERS_JOBS", "3")
    monkeypatch.setenv("RUNNERS_IDLE", "45s")

    target = env_target("owner/name")

    assert (target.token, target.label, target.jobs, target.idle) == (
        "ghp_env",
        "своя",
        3,
        45.0,
    )


def test_env_form_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(RunnerError):
        env_target("owner/name")


def test_an_empty_work_folder_is_refused() -> None:
    with pytest.raises(RunnerError, match="work"):
        Target(slug="owner/name", token="x", work="").check()


def test_jobs_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(RunnerError):
        load(_write(tmp_path, '[repos]\n"a/b" = { token = "t", jobs = 0 }\n'))
