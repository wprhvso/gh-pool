import pytest

from pool import sdk


class FakePool:
    def __init__(self, states=()):
        self.sent = []
        self.states = list(states)
        self.slept = []

    def call(self, method, path, **kwargs):
        self.sent.append((method, path, kwargs))
        return FakeAnswer(self._body(method, path))

    def _body(self, method, path):
        if method == "POST" and path == "/v1/tasks":
            return {"task_id": "t1"}
        if method == "GET" and path.endswith("/events"):
            return None
        if method == "GET" and path.startswith("/v1/tasks/"):
            return self.states.pop(0)
        return {}


class FakeAnswer:
    def __init__(self, body):
        self._body = body
        self.content = b"the last words\n"
        self.headers = {"X-Event-Size": str(len(self.content))}

    def json(self):
        return self._body


def double(x):
    return x * 2


def test_a_function_travels_as_its_own_source():
    remote = sdk.Remote(FakePool(), double)

    assert remote.code.startswith("def double(x):")
    assert remote.entry == "double"


def test_a_string_travels_as_it_is():
    remote = sdk.Remote(FakePool(), "result = 1")

    assert remote.code == "result = 1"
    assert remote.entry is None


def test_something_with_no_source_is_refused():
    with pytest.raises(TypeError):
        sdk.Remote(FakePool(), len)


def test_arguments_and_extras_ride_along_with_the_call():
    pool = FakePool()
    remote = sdk.Remote(pool, double, deps=["httpx"], timeout=30)

    remote.submit(21)

    _, _, kwargs = pool.sent[0]
    payload = kwargs["json"]["payload"]
    assert payload["args"] == [21]
    assert payload["entry"] == "double"
    assert payload["deps"] == ["httpx"]
    assert payload["timeout"] == 30


def test_nothing_optional_is_sent_when_there_is_nothing_to_send():
    pool = FakePool()

    sdk.Remote(pool, "result = 1").submit()

    payload = pool.sent[0][2]["json"]["payload"]
    assert "deps" not in payload
    assert "timeout" not in payload
    assert "entry" not in payload


def test_the_trace_context_rides_along_in_the_payload():
    pool = FakePool()

    sdk.Remote(pool, "result = 1").submit()

    assert isinstance(pool.sent[0][2]["json"]["payload"]["trace"], dict)


def test_waiting_stops_as_soon_as_the_task_is_finished(monkeypatch):
    monkeypatch.setattr(sdk.time, "sleep", lambda _: None)
    pool = FakePool([{"status": "running"}, {"status": "running"}, {"status": "done"}])

    state = sdk.Task(pool, "t1").wait()

    assert state["status"] == "done"
    assert len(pool.sent) == 3


def test_a_task_that_did_not_finish_well_raises(monkeypatch):
    monkeypatch.setattr(sdk.time, "sleep", lambda _: None)
    pool = FakePool([{"status": "failed", "error": "boom"}])

    with pytest.raises(sdk.Failed) as failure:
        sdk.Task(pool, "t1").check()

    assert "boom" in str(failure.value)


def test_the_poll_interval_backs_off_but_stops_growing(monkeypatch):
    waits = []
    monkeypatch.setattr(sdk.time, "sleep", waits.append)
    pool = FakePool([{"status": "running"}] * 30 + [{"status": "done"}])

    sdk.Task(pool, "t1").wait()

    assert waits[0] == pytest.approx(0.25)
    assert waits[-1] == pytest.approx(5.0)
    assert waits == sorted(waits)
