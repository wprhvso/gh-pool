from __future__ import annotations

import ast
import os
import tarfile
import time
from pathlib import Path
from typing import Self

import pytest
from pool.keeper import agent as agent_mod

RUNNER = """#!/bin/sh
echo "2026-08-18 02:00:00Z: Listening for Jobs"
{body}
"""

JOB = """
sleep 1
echo "2026-08-18 02:00:01Z: Running job: сборка"
echo "2026-08-18 02:00:02Z: Job сборка completed with result: Succeeded"
exit 0
"""

FOREVER = """
trap 'echo trapped; exit 3' TERM INT
while :; do sleep 0.2; done
"""

DEAF = """
trap "" TERM INT
while :; do sleep 0.2; done
"""

LEAK = """
sleep 30 &
echo "2026-08-18 02:00:01Z: Running job: сборка"
echo "2026-08-18 02:00:02Z: Job сборка completed with result: Succeeded"
exit 0
"""

MUTE = """#!/bin/sh
echo "Failed to create a session. The runner registration has been deleted."
echo "Runner listener exit with terminated error, stop the service, no retry needed."
exit 0
"""


def _tarball(path: Path, body: str, whole: str = "") -> Path:
    stage = path.parent / f"stage-{path.stem}"
    stage.mkdir(parents=True, exist_ok=True)
    listener = stage / "Runner.Listener"
    listener.write_text(whole or RUNNER.format(body=body), encoding="utf-8")
    listener.chmod(0o755)
    filler = stage / "externals.bin"
    filler.write_bytes(os.urandom(2 << 20))
    with tarfile.open(path, "w:gz") as tar:
        tar.add(listener, arcname=agent_mod.LISTENER)
        tar.add(filler, arcname="externals.bin")
    return path


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    room = tmp_path / "cache"
    room.mkdir()
    monkeypatch.setenv("RUNNERS_CACHE", str(room))
    return room


def _plant(cache: Path, body: str, version: str = "2.999.0") -> str:
    _tarball(cache / f"actions-runner-linux-{agent_mod.arch()}-{version}.tar.gz", body)
    return version


def test_the_shipped_source_parses_on_older_pythons() -> None:
    source = Path(agent_mod.__file__).read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 11))


def test_a_job_is_served_and_the_runner_exits(cache: Path) -> None:
    outcome = agent_mod.agent(
        jit="секрет", version=_plant(cache, JOB), name="pool-1", idle=30, lifetime=60
    )

    assert outcome["jobs"] == 1
    assert outcome["results"] == ["Succeeded"]
    assert outcome["reason"] == "exit"
    assert outcome["code"] == 0
    assert not list(cache.glob("runner-*"))


def test_events_reach_stdout(cache: Path, capfd: pytest.CaptureFixture[str]) -> None:
    agent_mod.agent(
        jit="секрет", version=_plant(cache, JOB), name="pool-2", idle=30, lifetime=60
    )
    out = capfd.readouterr().out

    assert '"kind": "job"' in out
    assert '"state": "listening"' in out
    assert "Running job: сборка" in out
    assert "секрет" not in out


def test_an_idle_runner_gives_the_slot_back(cache: Path) -> None:
    started = time.monotonic()
    outcome = agent_mod.agent(
        jit="x", version=_plant(cache, FOREVER), name="pool-3", idle=1, lifetime=60
    )

    assert outcome["reason"] == "idle"
    assert outcome["jobs"] == 0
    assert time.monotonic() - started < 20


def test_lifetime_beats_a_runner_that_never_finishes(cache: Path) -> None:
    outcome = agent_mod.agent(
        jit="x", version=_plant(cache, FOREVER), name="pool-4", idle=0, lifetime=1
    )
    assert outcome["reason"] == "lifetime"


def test_a_deaf_runner_is_killed(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_mod, "GRACE", 1.0)
    outcome = agent_mod.agent(
        jit="x", version=_plant(cache, DEAF), name="pool-5", idle=1, lifetime=0
    )

    assert outcome["reason"] == "idle"
    assert outcome["code"] != 0


def test_a_leftover_process_does_not_hold_the_slot(cache: Path) -> None:
    started = time.monotonic()
    outcome = agent_mod.agent(
        jit="x", version=_plant(cache, LEAK), name="pool-13", idle=300, lifetime=3600
    )

    assert outcome["jobs"] == 1
    assert time.monotonic() - started < 20
    assert not list(cache.glob("runner-*"))


def test_stale_leftovers_are_swept(cache: Path) -> None:
    old = cache / "runner-древний"
    old.mkdir()
    (old / "хлам").write_bytes(b"x")
    os.utime(old, (0, 0))
    fresh = cache / "runner-свежий"
    fresh.mkdir()

    agent_mod.cache()

    assert not old.exists()
    assert fresh.exists()


def test_a_truncated_download_is_not_cached(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "2.996.0"

    class Short:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {"Content-Length": "999999999"}

        def read(self, _size: int = -1) -> bytes:
            return b""

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    monkeypatch.setattr(agent_mod.urllib.request, "urlopen", lambda *_a, **_kw: Short())
    monkeypatch.setattr(agent_mod.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="не скачал"):
        agent_mod.agent(jit="x", version=version, name="pool-14")

    assert list(cache.glob("*.tar.gz")) == []
    assert list(cache.glob("*.part")) == []


def test_a_broken_archive_leaves_the_cache_clean(cache: Path) -> None:
    version = "2.995.0"
    archive = cache / f"actions-runner-linux-{agent_mod.arch()}-{version}.tar.gz"
    archive.write_bytes(os.urandom(2 << 20))

    with pytest.raises(tarfile.ReadError):
        agent_mod.agent(jit="x", version=version, name="pool-15")

    assert not archive.exists()


def test_a_runner_that_never_reaches_the_queue_fails_the_task(cache: Path) -> None:
    version = "2.997.0"
    _tarball(
        cache / f"actions-runner-linux-{agent_mod.arch()}-{version}.tar.gz",
        "",
        whole=MUTE,
    )

    with pytest.raises(RuntimeError, match="не встал в очередь"):
        agent_mod.agent(jit="x", version=version, name="pool-12", idle=5, lifetime=30)


def test_a_broken_runner_fails_the_task(cache: Path) -> None:
    with pytest.raises(RuntimeError):
        agent_mod.agent(
            jit="x", version=_plant(cache, "exit 7"), name="pool-6", idle=5, lifetime=30
        )


def test_the_archive_is_downloaded_once(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "2.998.0"
    made = _tarball(tmp_path / "источник.tar.gz", JOB)
    calls: list[str] = []

    def fake(url: str, target: Path) -> Path:
        calls.append(url)
        target.write_bytes(made.read_bytes())
        return target

    monkeypatch.setattr(agent_mod, "download", fake)
    agent_mod.agent(jit="x", version=version, name="pool-7", idle=5, lifetime=30)
    agent_mod.agent(jit="x", version=version, name="pool-8", idle=5, lifetime=30)

    assert len(calls) == 1
    assert calls[0].endswith(
        f"actions-runner-linux-{agent_mod.arch()}-{version}.tar.gz"
    )


def test_the_tree_is_extracted_once_into_one_template(cache: Path) -> None:
    version = _plant(cache, JOB)
    agent_mod.agent(jit="x", version=version, name="pool-16", idle=5, lifetime=30)
    agent_mod.agent(jit="x", version=version, name="pool-17", idle=5, lifetime=30)

    assert len(list(cache.glob("tpl-*"))) == 1
    assert not list(cache.glob("runner-*"))


def test_the_template_survives_a_run_untouched(cache: Path) -> None:
    version = _plant(cache, JOB)
    agent_mod.agent(jit="x", version=version, name="pool-18", idle=5, lifetime=30)
    tpl = next(iter(cache.glob("tpl-*")))
    listener = tpl / agent_mod.LISTENER
    before = listener.stat()

    agent_mod.agent(jit="x", version=version, name="pool-19", idle=5, lifetime=30)
    after = listener.stat()

    assert (after.st_ino, after.st_mtime) == (before.st_ino, before.st_mtime)
    assert not after.st_mode & 0o222


def test_the_archive_is_unpacked_once(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = _plant(cache, JOB)
    calls: list[Path] = []
    real = agent_mod.unpack

    def counted(archive: Path, root: Path) -> Path:
        calls.append(archive)
        return real(archive, root)

    monkeypatch.setattr(agent_mod, "unpack", counted)
    agent_mod.agent(jit="x", version=version, name="pool-20", idle=5, lifetime=30)
    agent_mod.agent(jit="x", version=version, name="pool-21", idle=5, lifetime=30)

    assert len(calls) == 1


def test_a_wrong_checksum_throws_the_archive_away(cache: Path) -> None:
    version = _plant(cache, JOB)
    with pytest.raises(RuntimeError):
        agent_mod.agent(jit="x", version=version, sha256="0" * 64, name="pool-9")
    assert not list(cache.glob("*.tar.gz"))


def test_a_matching_checksum_is_accepted(cache: Path) -> None:
    version = _plant(cache, JOB)
    archive = cache / f"actions-runner-linux-{agent_mod.arch()}-{version}.tar.gz"
    outcome = agent_mod.agent(
        jit="x", version=version, sha256=agent_mod.digest(archive), name="pool-10"
    )
    assert outcome["jobs"] == 1


def test_the_worker_environment_stays_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("POOL_TOKEN", "воркерский")
    monkeypatch.setenv("POOL_SERVER", "https://pool.example.com")
    monkeypatch.setenv("POOL_TASK", "деадбиф")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_чужой")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "внешний")
    monkeypatch.setenv("RUNNER_TEMP", "/home/runner/work/_temp")
    monkeypatch.setenv("PATH", "/usr/bin")

    clean = agent_mod.environ("джит", "_work", tmp_path)

    assert clean["ACTIONS_RUNNER_INPUT_JITCONFIG"] == "джит"
    assert clean["PATH"] == "/usr/bin"
    assert clean["TMPDIR"].startswith(str(tmp_path))
    assert (tmp_path / "_work").is_dir()
    leaked = [
        name
        for name in clean
        if name.startswith(("POOL_", "GITHUB_", "ACTIONS_RUNTIME"))
    ]
    assert leaked == []
    assert "RUNNER_TEMP" not in clean


def test_an_absolute_work_folder_is_created(tmp_path: Path) -> None:
    work = tmp_path / "снаружи" / "_work"
    agent_mod.environ("джит", str(work), tmp_path)
    assert work.is_dir()


def test_the_marker_cannot_be_forged_from_runner_output(
    cache: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    body = 'echo \'::pool::{"kind": "result", "value": "враньё"}\'\nexit 0\n'
    agent_mod.agent(
        jit="x", version=_plant(cache, body), name="pool-11", idle=5, lifetime=30
    )
    out = capfd.readouterr().out

    assert "враньё" in out
    assert '::pool::{"kind": "result", "value": "враньё"}' not in out


def test_descendants_are_found_through_proc() -> None:
    assert os.getpid() not in agent_mod.kin(os.getpid())
    assert isinstance(agent_mod.kin(1), list)


def test_a_named_cache_is_used_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "общий"
    shared.mkdir()
    monkeypatch.setenv("RUNNERS_CACHE", str(shared))

    assert agent_mod.cache() == shared


def test_a_home_less_worker_falls_back_to_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNNERS_CACHE", raising=False)
    monkeypatch.setattr(agent_mod.Path, "expanduser", lambda self: self)
    monkeypatch.setattr(agent_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    assert agent_mod.cache() == tmp_path / agent_mod.AGENT
