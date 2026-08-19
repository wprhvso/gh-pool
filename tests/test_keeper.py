import email.message
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
        self.asked = []

    def api(self, path, method="GET", body=None):
        if path == "":
            return {"default_branch": "main"}
        if "/runs?status=" in path:
            status = path.split("status=")[1].split("&")[0]
            self.asked.append(status)
            return {"workflow_runs": [x for x in self.runs if x["status"] == status]}
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

    keeper.reconcile(repo, {1: 30000.0, 2: 10.0})

    assert repo.cancelled == [1]


def serving(runs, seconds=60.0) -> dict[int, float]:
    return {int(x["id"]): seconds for x in runs}


def test_a_full_fleet_launches_nothing():
    warmed = [run(i, 500) for i in range(20)]
    repo = FakeRepo(warmed, jobs=20)

    keeper.reconcile(repo, serving(warmed))

    assert repo.dispatched == 0


def test_a_warmed_run_that_never_serves_frees_its_place():
    warmed = [run(i, 1000) for i in range(20)]
    repo = FakeRepo(warmed, jobs=20)

    keeper.reconcile(repo, serving(warmed[:15]))

    assert repo.dispatched == 5


def test_runs_that_are_still_warming_up_are_counted_as_alive():
    young = [run(i, 10) for i in range(20)]
    repo = FakeRepo(young, jobs=20)

    keeper.reconcile(repo, {})

    assert repo.dispatched == 0


def test_no_more_than_the_launch_budget_goes_out_at_once(monkeypatch):
    monkeypatch.setattr(keeper, "MAX_LAUNCH", 5)
    repo = FakeRepo([], jobs=20)

    keeper.reconcile(repo, {})

    assert repo.dispatched == 5


def test_the_ttl_is_measured_from_when_the_worker_began_serving():
    # Прогон мог сутки простоять в очереди: его created_at ничего не говорит
    # о возрасте воркера, и раньше такой воркер гасился сразу, как живой.
    long_queued = run(1, 100000)
    repo = FakeRepo([long_queued], jobs=20)

    keeper.reconcile(repo, {1: 60.0})

    assert repo.cancelled == []


def test_a_worker_that_has_served_past_its_ttl_is_retired():
    old = run(1, 100000)
    repo = FakeRepo([old], jobs=20, ttl="6h")

    keeper.reconcile(repo, {1: 25000.0})

    assert repo.cancelled == [1]


def test_while_the_pool_is_silent_a_busy_run_is_left_alone():
    # Прогрев сервера открывал окно, в котором занятые гасились по created_at.
    repo = FakeRepo([run(1, 30000), run(2, 90000)], jobs=20, ttl="6h")

    keeper.reconcile(repo, None)

    assert repo.cancelled == []


def test_a_run_is_never_cancelled_twice():
    repo = FakeRepo([run(1, 100000), run(2, 10)], jobs=20)

    keeper.reconcile(repo, {1: 30000.0, 2: 10.0})
    keeper.reconcile(repo, {1: 30000.0, 2: 10.0})

    assert repo.cancelled == [1]


def test_a_freshly_dispatched_run_is_not_dispatched_again(monkeypatch):
    monkeypatch.setattr(keeper, "MAX_LAUNCH", 20)
    repo = FakeRepo([], jobs=3)

    keeper.reconcile(repo, {})
    keeper.reconcile(repo, {})

    assert repo.dispatched == 3


def test_every_unfinished_status_is_asked_for_by_name():
    repo = FakeRepo([run(1, 10)], jobs=20)

    keeper.reconcile(repo)

    assert repo.asked == list(keeper.STATUSES)


def test_a_long_lived_run_is_seen_even_behind_a_wall_of_newer_ones():
    old = run(1, 20000)
    newer = [run(100 + i, 10 + i, "queued") for i in range(20)]
    repo = FakeRepo([*newer, old], jobs=20)

    keeper.reconcile(repo, {1: 60.0})

    assert repo.dispatched == 0
    assert repo.cancelled == [100]


def test_a_run_without_a_machine_is_dropped_before_a_serving_one():
    working, waiting = run(1, 3000), run(2, 10, "queued")
    repo = FakeRepo([waiting, working], jobs=1)

    keeper.reconcile(repo, {1: 3000.0})

    assert repo.cancelled == [2]


def test_runs_without_a_machine_do_not_cost_the_retire_budget():
    repo = FakeRepo([run(i, 10 + i, "queued") for i in range(6)], jobs=2)

    keeper.reconcile(repo, {})

    assert len(repo.cancelled) == 4


def test_a_queued_run_holds_a_place_but_does_not_count_as_serving():
    repo = FakeRepo([run(i, 10, "queued") for i in range(20)], jobs=20)

    keeper.reconcile(repo, {})

    assert repo.dispatched == 0
    assert repo.cancelled == []


class FakeAnswer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def fake_pool(monkeypatch, listing, up=9999):
    def urlopen(req, **_):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/healthz"):
            return FakeAnswer({"started_at": time.time() - up})
        return FakeAnswer(listing)

    monkeypatch.setattr(keeper.urllib.request, "urlopen", urlopen)


POOL = {"POOL_SERVER": "http://pool"}


def test_the_workers_are_matched_to_their_runs_by_id(monkeypatch):
    fake_pool(
        monkeypatch,
        [
            {"id": "gh-4242", "serving_for": 61.5},
            {"id": "gh-777", "serving_for": 10.0},
        ],
    )

    assert keeper.pool_workers(POOL, "client") == {4242: 61.5, 777: 10.0}


def test_a_worker_whose_name_holds_no_run_id_is_skipped(monkeypatch):
    fake_pool(monkeypatch, [{"id": "laptop", "serving_for": 5.0}])

    assert keeper.pool_workers(POOL, "client") == {}


def test_a_pool_that_has_just_started_is_not_believed(monkeypatch):
    fake_pool(monkeypatch, [], up=0)

    assert keeper.pool_workers(POOL, "client") is None


def test_a_pool_that_does_not_answer_is_not_believed(monkeypatch):
    def refuse(*_, **__):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(keeper.urllib.request, "urlopen", refuse)

    assert keeper.pool_workers(POOL, "client") is None


def test_without_a_pool_address_there_is_nothing_to_ask():
    assert keeper.pool_workers({}, "client") is None


def test_the_idle_backlog_is_drained_in_bites(monkeypatch):
    monkeypatch.setattr(keeper, "IDLE_BUDGET", 3)
    repo = FakeRepo([run(i, 10 + i, "queued") for i in range(30)], jobs=2)

    keeper.reconcile(repo, {})

    assert len(repo.cancelled) == 3


def test_a_refused_request_is_not_mistaken_for_a_dead_pool(monkeypatch):
    def refuse(req, **_):
        raise urllib.error.HTTPError(
            req.full_url, 401, "no", email.message.Message(), None
        )

    monkeypatch.setattr(keeper.urllib.request, "urlopen", refuse)
    said = []
    monkeypatch.setattr(keeper, "log", said.append)

    assert keeper.pool_workers(POOL, "wrong") is None
    assert "client_token" in said[0]


def test_without_a_client_token_the_workers_are_not_asked_for(monkeypatch):
    monkeypatch.setattr(
        keeper.urllib.request, "urlopen", lambda *a, **k: pytest.fail("не спрашивать")
    )

    assert keeper.pool_workers(POOL, "") is None


def test_the_client_token_is_not_propagated_to_repositories(tmp_path):
    cfg = tmp_path / "keeper.toml"
    cfg.write_text(
        '[pool]\nserver = "http://pool"\ntoken = "worker"\nclient_token = "client"\n'
        '\n[repos]\n"a/b" = { token = "gh" }\n'
    )

    _, _, pool, client = keeper.load(cfg)

    assert client == "client"
    assert "POOL_CLIENT_TOKEN" not in pool
    assert pool == {"POOL_SERVER": "http://pool", "POOL_TOKEN": "worker"}
