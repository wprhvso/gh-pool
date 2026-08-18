import json
import subprocess

import pytest

from pool import rpc, tasks


@pytest.fixture
def emitted(monkeypatch):
    seen = []
    monkeypatch.setattr(rpc, "emit", lambda kind, *a, **k: seen.append((kind, a, k)))
    monkeypatch.setattr(tasks.rpc, "emit", rpc.emit)
    return seen


def kinds(emitted):
    return [kind for kind, _, _ in emitted]


def values(emitted, kind):
    return [a[0] for k, a, _ in emitted if k == kind and a]


def test_a_bare_script_returns_what_it_left_in_result(emitted):
    tasks.run({"code": "result = 2 + 2"})

    assert values(emitted, "result") == [4]


def test_an_entry_point_is_called_with_the_arguments_it_was_given(emitted):
    tasks.run(
        {
            "code": "def add(a, b=0):\n    return a + b\n",
            "entry": "add",
            "args": [3],
            "kwargs": {"b": 4},
        }
    )

    assert values(emitted, "result") == [7]


def test_a_script_that_returns_nothing_says_nothing(emitted):
    tasks.run({"code": "result = None"})

    assert kinds(emitted) == []


def test_an_entry_point_the_code_never_defined_is_an_error(emitted):
    with pytest.raises(NameError):
        tasks.run({"code": "x = 1", "entry": "missing"})


def test_a_value_that_is_not_json_is_refused(emitted):
    with pytest.raises(TypeError):
        tasks.run({"code": "result = object()"})


def test_a_task_that_overruns_its_timeout_is_stopped(emitted):
    with pytest.raises(TimeoutError):
        tasks.run({"code": "import time\ntime.sleep(5)", "timeout": 0.1})


def test_a_failure_is_reported_before_it_is_raised(emitted):
    with pytest.raises(ZeroDivisionError):
        tasks.python({"code": "result = 1 / 0"})

    assert kinds(emitted) == ["error"]


def test_the_traceback_points_at_the_submitted_code(emitted):
    with pytest.raises(ZeroDivisionError) as failure:
        tasks.run({"code": "\n\nresult = 1 / 0\n"})

    assert failure.traceback[-1].lineno == 2


def test_dependencies_are_installed_once_and_remembered(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, "DEPS", tmp_path / "deps")
    calls = []

    def record(command, **kwargs):
        calls.append(command)
        target = command[command.index("--target") + 1]
        __import__("pathlib").Path(target).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tasks.subprocess, "run", record)

    tasks.install(["httpx==1.0"])
    tasks.install(["httpx==1.0"])

    assert len(calls) == 1


def test_a_different_set_of_dependencies_is_installed_on_its_own(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, "DEPS", tmp_path / "deps")
    calls = []

    def record(command, **kwargs):
        calls.append(command)
        target = command[command.index("--target") + 1]
        __import__("pathlib").Path(target).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tasks.subprocess, "run", record)

    tasks.install(["httpx==1.0"])
    tasks.install(["httpx==2.0"])

    assert len(calls) == 2


def test_the_order_dependencies_are_written_in_does_not_matter(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, "DEPS", tmp_path / "deps")
    calls = []

    def record(command, **kwargs):
        calls.append(command)
        target = command[command.index("--target") + 1]
        __import__("pathlib").Path(target).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tasks.subprocess, "run", record)

    tasks.install(["a", "b"])
    tasks.install(["b", "a"])

    assert len(calls) == 1


def test_an_event_carries_its_kind_and_value():
    event = rpc.emit("result", {"ok": True})

    assert event["kind"] == "result"
    assert event["value"] == {"ok": True}
    assert json.dumps(event)
