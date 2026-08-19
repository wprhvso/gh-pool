import argparse
import json

import httpx
import pytest

from pool import cli


class Fake:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.seen = []

    def request(self, method, path, **kw):
        self.seen.append((method, path, kw))
        if not self.answers:
            return httpx.Response(200, json={})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(cli, "http", f)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    return f


def args(**kw):
    return argparse.Namespace(**kw)


def events(content=b"", offset=0, status="running", size=None):
    return httpx.Response(
        200,
        content=content,
        headers={
            "X-Event-Offset": str(offset + len(content)),
            "X-Task-Status": status,
            "X-Event-Size": str(len(content) if size is None else size),
        },
    )


def test_a_payload_is_built_from_the_pairs_on_the_command_line():
    got = cli.parse_payload(args(payload=None, payload_file=None, set=["a=1", "b=hi"]))

    assert got == {"a": 1, "b": "hi"}


def test_a_pair_whose_value_is_json_keeps_its_shape():
    got = cli.parse_payload(args(payload=None, payload_file=None, set=['a=[1, "x"]']))

    assert got == {"a": [1, "x"]}


def test_a_pair_without_an_equals_sign_is_refused():
    with pytest.raises(SystemExit):
        cli.parse_payload(args(payload=None, payload_file=None, set=["oops"]))


def test_a_whole_payload_can_be_given_at_once():
    got = cli.parse_payload(args(payload='{"code": "x"}', payload_file=None, set=None))

    assert got == {"code": "x"}


def test_a_payload_can_come_from_a_file(tmp_path):
    p = tmp_path / "payload.json"
    p.write_text('{"code": "from a file"}')

    got = cli.parse_payload(args(payload=None, payload_file=str(p), set=None))

    assert got == {"code": "from a file"}


def test_nothing_on_the_command_line_means_an_empty_payload():
    assert cli.parse_payload(args(payload=None, payload_file=None, set=None)) == {}


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (30, "30s"), (90, "1m"), (7200, "2h"), (172800, "2d")],
)
def test_an_age_is_written_in_the_largest_unit_that_fits(seconds, text, monkeypatch):
    monkeypatch.setattr(cli.time, "time", lambda: 1000.0)

    assert cli.ago(1000.0 - seconds) == text


def test_an_age_that_was_never_set_is_a_dash():
    assert cli.ago(None) == "-"


def test_a_server_that_cannot_be_reached_is_said_so(fake, capsys):
    fake.answers = [httpx.ConnectError("refused")]

    with pytest.raises(SystemExit):
        cli.call("GET", "/healthz")

    assert "server unreachable" in capsys.readouterr().err


def test_a_refusal_from_the_server_stops_the_command(fake, capsys):
    fake.answers = [httpx.Response(401, text="bad client token")]

    with pytest.raises(SystemExit):
        cli.call("GET", "/healthz")

    assert "401" in capsys.readouterr().err


def test_following_prints_every_chunk_until_the_task_is_over(fake, capsys):
    fake.answers = [
        events(b"first\n", 0, "running"),
        events(b"second\n", 6, "done", size=13),
        events(b"", 13, "done", size=13),
    ]

    assert cli.follow("t1") == "done"
    assert capsys.readouterr().out == "first\nsecond\n"


def test_following_keeps_reading_while_the_tail_has_not_arrived(fake):
    fake.answers = [
        events(b"tail\n", 0, "done", size=99),
        events(b"", 5, "done", size=5),
    ]

    assert cli.follow("t1") == "done"
    assert len(fake.seen) == 2


def test_following_starts_from_the_offset_it_was_given(fake):
    fake.answers = [events(b"", 40, "done", size=40), events(b"", 40, "done", size=40)]

    cli.follow("t1", offset=40)

    assert fake.seen[0][2]["params"] == {"offset": 40}


@pytest.mark.parametrize(
    ("status", "code"),
    [("done", 0), ("failed", 1), ("cancelled", 2), ("lost", 3), (None, 1)],
)
def test_the_exit_code_says_how_the_task_ended(fake, status, code):
    fake.answers = [httpx.Response(200, json={"error": None})]

    with pytest.raises(SystemExit) as exit:
        cli.finish("t1", status)

    assert exit.value.code == code


def test_submitting_prints_the_task_id(fake, capsys):
    fake.answers = [httpx.Response(200, json={"task_id": "abc"})]

    cli.cmd_submit(
        args(type="python", payload=None, payload_file=None, set=None, follow=False)
    )

    assert capsys.readouterr().out.strip() == "abc"


def test_a_submitted_payload_carries_the_trace_along(fake):
    fake.answers = [httpx.Response(200, json={"task_id": "abc"})]

    cli.cmd_submit(
        args(
            type="python", payload=None, payload_file=None, set=["code=1"], follow=False
        )
    )

    body = fake.seen[0][2]["json"]
    assert body["type"] == "python"
    assert "trace" in body["payload"]


def test_an_empty_listing_says_so_rather_than_printing_a_header(fake, capsys):
    fake.answers = [httpx.Response(200, json=[])]

    cli.cmd_list(args(status=None, limit=30))

    assert capsys.readouterr().out.strip() == "nothing"


def test_a_listing_shows_a_row_for_every_task(fake, capsys):
    fake.answers = [
        httpx.Response(
            200,
            json=[
                {
                    "id": "t1",
                    "type": "python",
                    "status": "done",
                    "created_at": None,
                    "event_size": 12,
                    "worker_id": "w1",
                }
            ],
        )
    ]

    cli.cmd_list(args(status="done", limit=30))

    out = capsys.readouterr().out
    assert "t1" in out and "python" in out and "w1" in out
    assert fake.seen[0][2]["params"] == {"limit": 30, "status": "done"}


def test_an_empty_artifact_listing_says_so(fake, capsys):
    fake.answers = [httpx.Response(200, json=[])]

    cli.cmd_artifacts(args(prefix="", limit=30))

    assert capsys.readouterr().out.strip() == "nothing"


def test_the_artifact_listing_shows_key_size_and_task(fake, capsys):
    fake.answers = [
        httpx.Response(
            200,
            json=[{"key": "out/a", "size": 7, "created_at": None, "task_id": "t1"}],
        )
    ]

    cli.cmd_artifacts(args(prefix="out/", limit=30))

    out = capsys.readouterr().out
    assert "out/a" in out and "7" in out and "t1" in out


def test_no_workers_is_stated_plainly(fake, capsys):
    fake.answers = [httpx.Response(200, json=[])]

    cli.cmd_workers(args())

    assert capsys.readouterr().out.strip() == "no workers"


def test_each_worker_is_listed_with_what_it_holds(fake, capsys):
    fake.answers = [
        httpx.Response(200, json=[{"id": "w1", "idle_for": 1.2, "task_id": "t1"}])
    ]

    cli.cmd_workers(args())

    assert "w1" in capsys.readouterr().out


def test_a_status_is_printed_as_readable_json(fake, capsys):
    fake.answers = [httpx.Response(200, json={"id": "t1", "status": "done"})]

    cli.cmd_status(args(id="t1"))

    assert json.loads(capsys.readouterr().out)["status"] == "done"


def test_cancelling_prints_what_the_server_answered(fake, capsys):
    fake.answers = [httpx.Response(200, json={"status": "cancelled"})]

    cli.cmd_cancel(args(id="t1"))

    assert json.loads(capsys.readouterr().out) == {"status": "cancelled"}
    assert fake.seen[0][:2] == ("POST", "/v1/tasks/t1/cancel")


def test_a_retry_prints_the_new_task_id(fake, capsys):
    fake.answers = [httpx.Response(200, json={"task_id": "t2"})]

    cli.cmd_retry(args(id="t1", follow=False))

    assert capsys.readouterr().out.strip() == "t2"


def test_reading_events_without_following_writes_them_once(fake, capsys):
    fake.answers = [events(b"the output\n", 0, "done")]

    cli.cmd_events(args(id="t1", follow=False, offset=0))

    assert capsys.readouterr().out == "the output\n"


def test_an_artifact_is_uploaded_from_a_file(fake, tmp_path, capsys):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"payload")
    fake.answers = [httpx.Response(200, json={"key": "k", "size": 7})]

    cli.cmd_put(args(key="k", file=str(f), task="t1"))

    assert fake.seen[0][:2] == ("PUT", "/v1/artifacts/k")
    assert fake.seen[0][2]["params"] == {"task_id": "t1"}
    assert json.loads(capsys.readouterr().out)["size"] == 7


def test_removing_an_artifact_asks_the_server_to_delete_it(fake, capsys):
    fake.answers = [httpx.Response(200, json={"ok": True})]

    cli.cmd_rm(args(key="k"))

    assert fake.seen[0][:2] == ("DELETE", "/v1/artifacts/k")
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_health_is_printed_as_readable_json(fake, capsys):
    fake.answers = [httpx.Response(200, json={"ok": True})]

    cli.cmd_health(args())

    assert json.loads(capsys.readouterr().out) == {"ok": True}
