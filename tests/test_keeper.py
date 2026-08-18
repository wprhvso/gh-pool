import json
import time
import urllib.error

import pytest

from pool import keeper


class FakeRepo(keeper.Repo):
    def __init__(self, runs, **kwargs):
        super().__init__(slug="wprhvso/pool", token="t", **kwargs)
        self.runs = runs
        self.cancelled = []
        self.dispatched = 0

    def api(self, path, method="GET", body=None):
        if path == "":
            return {"default_branch": "main"}
        if path.endswith("/runs?per_page=100"):
            return {"workflow_runs": self.runs}
        if path.endswith("/cancel"):
            self.cancelled.append(int(path.split("/")[3]))
            return {}
        if path.endswith("/dispatches"):
            self.dispatched += 1
            return {}
        raise AssertionError(f"unexpected call {method} {path} {body}")


def run(run_id, age_seconds, status="in_progress"):
    created = time.time() - age_seconds
    return {
        "id": run_id,
        "status": status,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
    }


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("6h", 21600), ("2d", 172800), (90, 90), (1.5, 1.5)],
)
def test_durations_are_read_with_their_unit(text, seconds):
    assert keeper.secs(text) == seconds


def test_a_duration_string_without_a_unit_is_not_understood():
    with pytest.raises(KeyError):
        keeper.secs("45")


def test_surplus_is_taken_from_the_youngest_runs(monkeypatch):
    monkeypatch.setattr(keeper, "RETIRE_BUDGET", 10)
    oldest, middle, newest = run(1, 5000), run(2, 3000), run(3, 10)
    repo = FakeRepo([newest, middle, oldest], jobs=2)

    keeper.reconcile(repo)

    assert repo.cancelled == [3]


def test_only_one_run_is_retired_in_a_tick():
    repo = FakeRepo([run(i, 10 + i) for i in range(6)], jobs=2)

    keeper.reconcile(repo)

    assert len(repo.cancelled) == 1


def test_a_run_past_its_ttl_is_retired():
    repo = FakeRepo([run(1, 100000), run(2, 10)], jobs=20, ttl="6h")

    keeper.reconcile(repo)

    assert repo.cancelled == [1]


def test_a_full_fleet_launches_nothing():
    repo = FakeRepo([run(i, 500) for i in range(20)], jobs=20)

    keeper.reconcile(repo, serving=20)

    assert repo.dispatched == 0


def test_a_warmed_run_that_never_serves_frees_its_place():
    warmed = [run(i, 1000) for i in range(20)]
    repo = FakeRepo(warmed, jobs=20)

    keeper.reconcile(repo, serving=15)

    assert repo.dispatched == 5


def test_runs_that_are_still_warming_up_are_counted_as_alive():
    young = [run(i, 10) for i in range(20)]
    repo = FakeRepo(young, jobs=20)

    keeper.reconcile(repo, serving=0)

    assert repo.dispatched == 0


def test_no_more_than_the_launch_budget_goes_out_at_once(monkeypatch):
    monkeypatch.setattr(keeper, "MAX_LAUNCH", 5)
    repo = FakeRepo([], jobs=20)

    keeper.reconcile(repo, serving=0)

    assert repo.dispatched == 5


def test_without_a_serving_count_the_runs_are_trusted_as_before():
    warmed = [run(i, 1000) for i in range(20)]
    repo = FakeRepo(warmed, jobs=20)

    keeper.reconcile(repo, serving=None)

    assert repo.dispatched == 0


def test_a_run_is_never_cancelled_twice():
    repo = FakeRepo([run(1, 100000), run(2, 10)], jobs=20)

    keeper.reconcile(repo)
    keeper.reconcile(repo)

    assert repo.cancelled == [1]


def test_a_freshly_dispatched_run_is_not_dispatched_again(monkeypatch):
    monkeypatch.setattr(keeper, "MAX_LAUNCH", 20)
    repo = FakeRepo([], jobs=3)

    keeper.reconcile(repo, serving=0)
    keeper.reconcile(repo, serving=0)

    assert repo.dispatched == 3


class FakeAnswer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_the_serving_count_comes_from_the_pool(monkeypatch):
    monkeypatch.setattr(
        keeper.urllib.request,
        "urlopen",
        lambda *a, **k: FakeAnswer({"workers": 17, "started_at": time.time() - 9999}),
    )

    assert keeper.pool_serving({"POOL_SERVER": "http://pool"}) == 17


def test_a_pool_that_has_just_started_is_not_believed(monkeypatch):
    monkeypatch.setattr(
        keeper.urllib.request,
        "urlopen",
        lambda *a, **k: FakeAnswer({"workers": 0, "started_at": time.time()}),
    )

    assert keeper.pool_serving({"POOL_SERVER": "http://pool"}) is None


def test_a_pool_that_does_not_answer_is_not_believed(monkeypatch):
    def refuse(*_, **__):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(keeper.urllib.request, "urlopen", refuse)

    assert keeper.pool_serving({"POOL_SERVER": "http://pool"}) is None


def test_without_a_pool_address_there_is_nothing_to_ask():
    assert keeper.pool_serving({}) is None
