import argparse
import json

import httpx
import pytest

from gh_pool import cli


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
    monkeypatch.setattr(cli, "http", lambda: f)
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

    with pytest.raises(SystemExit) as left:
        cli.finish("t1", status)

    assert left.value.code == code


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
    assert "t1" in out
    assert "python" in out
    assert "w1" in out
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
    assert "out/a" in out
    assert "7" in out
    assert "t1" in out


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


SESSION = "6f1b8f7a-1111-2222-3333-444455556666"


@pytest.fixture
def chrome(monkeypatch):
    monkeypatch.setattr(cli, "CHROME_TOKEN", "chrome-secret")
    opened = []

    def open_new(url):
        opened.append(url)
        return True

    monkeypatch.setattr(cli.chrome.webbrowser, "open_new", open_new)
    return opened


def chrome_args(**kw):
    return args(
        **{
            "profile": None,
            "session": None,
            "no_open": False,
            "player": False,
            "wait": 0.0,
            "width": None,
            "height": None,
        }
        | kw
    )


def session(status="active"):
    return httpx.Response(200, json={"id": SESSION, "status": status})


def test_the_browser_api_is_asked_with_its_own_token(fake, chrome):
    cli.chrome_call("GET", "/sessions")

    assert fake.seen[0][2]["headers"]["Authorization"] == "Bearer chrome-secret"


def test_without_a_browser_token_the_command_stops(fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CHROME_TOKEN", "")

    with pytest.raises(SystemExit):
        cli.chrome_call("GET", "/sessions")

    assert "GH_POOL_TOKEN" in capsys.readouterr().err


def test_a_chrome_session_is_opened_on_the_profile_it_was_given(fake, chrome):
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args(profile="shopping"))

    assert fake.seen[0][:2] == ("POST", "/sessions")
    assert fake.seen[0][2]["json"] == {"profile": "shopping"}


def test_a_size_on_the_command_line_reaches_the_session(fake, chrome):
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args(width=1280, height=720))

    assert fake.seen[0][2]["json"]["params"] == {"width": 1280, "height": 720}


def test_the_desktop_link_is_printed_and_opened_in_a_new_browser(fake, chrome, capsys):
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args())

    printed = capsys.readouterr().out.strip()
    assert (
        printed
        == f"{cli.SERVER}/s/{SESSION}/vnc/?path=s%2F{SESSION}%2Fvnc%2Fwebsockify&resize=scale&reconnect=true&reconnect_delay=2000&reconnect_retries=1000"
    )
    assert chrome == [printed]


def test_the_player_link_is_opened_instead_when_it_is_asked_for(fake, chrome, capsys):
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args(player=True))

    printed = capsys.readouterr().out.strip()
    assert printed == f"{cli.SERVER}/s/{SESSION}"
    assert chrome == [printed]


def test_the_login_and_the_player_are_said_alongside_the_link(fake, chrome, capsys):
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args())

    err = capsys.readouterr().err
    assert SESSION in err
    assert "admin" in err
    assert f"{cli.SERVER}/s/{SESSION}" in err


def test_the_link_can_be_printed_without_opening_anything(fake, chrome, capsys):
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args(no_open=True))

    assert SESSION in capsys.readouterr().out
    assert chrome == []


def test_printing_the_link_does_not_wait_for_the_desktop(fake, chrome, capsys):
    fake.answers = [session("pending")]

    cli.cmd_chrome(chrome_args(session=SESSION, no_open=True, wait=600.0))

    assert fake.seen == []
    assert SESSION in capsys.readouterr().out


def test_an_existing_session_is_opened_rather_than_a_new_one(fake, chrome, capsys):
    cli.cmd_chrome(chrome_args(session=SESSION))

    assert fake.seen == []
    assert SESSION in capsys.readouterr().out
    assert chrome == [cli.chrome.desktop(cli.SERVER, SESSION)]


def test_an_existing_session_and_a_profile_together_are_refused(fake, chrome):
    with pytest.raises(SystemExit):
        cli.cmd_chrome(chrome_args(session=SESSION, profile="shopping"))

    assert fake.seen == []


def test_waiting_holds_until_the_runner_brings_the_desktop_up(fake, chrome, capsys):
    fake.answers = [session("pending"), session("pending"), session("active")]

    cli.cmd_chrome(chrome_args(session=SESSION, wait=600.0))

    assert [call[:2] for call in fake.seen] == [("GET", f"/sessions/{SESSION}")] * 3
    assert chrome == [cli.chrome.desktop(cli.SERVER, SESSION)]


@pytest.mark.parametrize("status", ["closed", "dead"])
def test_a_session_that_is_over_has_no_desktop_to_open(fake, chrome, status):
    fake.answers = [session(status)]

    with pytest.raises(SystemExit):
        cli.cmd_chrome(chrome_args(session=SESSION, wait=600.0))

    assert chrome == []


def test_a_desktop_that_never_came_up_is_opened_anyway(
    fake, chrome, capsys, monkeypatch
):
    clock = iter([0.0, 1e9])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    fake.answers = [session("pending")]

    cli.cmd_chrome(chrome_args(session=SESSION, wait=600.0))

    assert "no desktop yet" in capsys.readouterr().err
    assert chrome == [cli.chrome.desktop(cli.SERVER, SESSION)]


def test_a_machine_with_no_browser_on_it_says_so(fake, chrome, monkeypatch, capsys):
    monkeypatch.setattr(cli.chrome.webbrowser, "open_new", lambda url: False)
    fake.answers = [session()]

    cli.cmd_chrome(chrome_args())

    assert "no browser here" in capsys.readouterr().err


def test_a_browser_that_refuses_to_start_is_not_an_error(monkeypatch):
    def boom(url):
        raise OSError("no display")

    monkeypatch.setattr(cli.chrome.webbrowser, "open_new", boom)

    assert cli.chrome.open_new("https://example.com") is False
