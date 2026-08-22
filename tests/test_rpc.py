import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import unquote

import pytest

from gh_pool import rpc


class Store(BaseHTTPRequestHandler):
    blobs: ClassVar[dict[str, bytes]] = {}
    calls: ClassVar[list[tuple[str, str, str]]] = []

    def _read(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _record(self):
        Store.calls.append(
            (self.command, self.path, self.headers.get("Authorization", ""))
        )

    def do_PUT(self):
        self._record()
        key = unquote(self.path.split("?")[0]).removeprefix("/v1/artifacts/")
        body = self._read()
        Store.blobs[key] = body
        self._answer(json.dumps({"size": len(body), "sha256": "deadbeef"}).encode())

    def do_GET(self):
        self._record()
        key = self.path.removeprefix("/v1/artifacts/")
        if key not in Store.blobs:
            self.send_response(404)
            self.end_headers()
            return
        self._answer(Store.blobs[key])

    def _answer(self, body):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def store(monkeypatch):
    Store.blobs = {}
    Store.calls = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Store)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(rpc, "SERVER", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setattr(rpc, "TOKEN", "dev-worker")
    monkeypatch.setattr(rpc, "TASK", None)
    yield Store
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_an_event_is_a_marked_line_of_json(capsys):
    event = rpc.emit("note", "hello")

    line = capsys.readouterr().out
    assert line.startswith(rpc.MARK)
    assert line.endswith("\n")
    assert json.loads(line[len(rpc.MARK) :]) == event
    assert event["kind"] == "note"
    assert event["value"] == "hello"


def test_an_event_without_a_value_carries_none(capsys):
    event = rpc.emit("ping")

    assert "value" not in event
    assert "value" not in capsys.readouterr().out


def test_an_event_carries_the_extra_fields_it_was_given(capsys):
    rpc.emit("artifact", key="k", size=3)

    event = rpc.parse(capsys.readouterr().out)[0]
    assert event["key"] == "k"
    assert event["size"] == 3


def test_a_value_json_cannot_hold_is_written_as_text(capsys):
    rpc.emit("result", object())

    assert isinstance(rpc.parse(capsys.readouterr().out)[0]["value"], str)


def test_events_from_many_threads_stay_whole(capsys):
    def shout(n):
        for i in range(50):
            rpc.emit("note", f"{n}-{i}")

    threads = [threading.Thread(target=shout, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(rpc.parse(capsys.readouterr().out)) == 400


def test_plain_output_is_not_mistaken_for_an_event():
    text = "just a log line\n" + rpc.MARK + '{"kind": "result"}\n' + "and more\n"

    assert rpc.parse(text) == [{"kind": "result"}]


def test_a_broken_event_is_skipped_rather_than_fatal():
    text = rpc.MARK + "{not json}\n" + rpc.MARK + '{"kind": "ok"}\n'

    assert rpc.parse(text) == [{"kind": "ok"}]


def test_nothing_in_nothing_out():
    assert rpc.parse("") == []


def test_bytes_are_stored_under_their_key(store, capsys):
    meta = rpc.put("out/report.txt", b"the report")

    assert store.blobs["out/report.txt"] == b"the report"
    assert meta["size"] == 10
    assert rpc.parse(capsys.readouterr().out)[0]["kind"] == "artifact"


def test_text_is_stored_as_its_bytes(store):
    rpc.put("k", "привет")

    assert store.blobs["k"] == "привет".encode()


def test_a_file_is_streamed_from_disk(store, tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 5000)

    assert rpc.put("k", p)["size"] == 5000
    assert store.blobs["k"] == b"x" * 5000


def test_an_open_file_is_read_to_the_end(store, tmp_path):
    p = tmp_path / "open.bin"
    p.write_bytes(b"from a handle")

    with p.open("rb") as f:
        rpc.put("k", f)

    assert store.blobs["k"] == b"from a handle"


def test_the_task_that_is_running_is_attached_by_default(store, monkeypatch):
    monkeypatch.setattr(rpc, "TASK", "t42")
    rpc.put("k", b"x")

    assert "task_id=t42" in store.calls[0][1]


def test_a_task_given_by_hand_wins(store, monkeypatch):
    monkeypatch.setattr(rpc, "TASK", "t42")
    rpc.put("k", b"x", task_id="t7")

    assert "task_id=t7" in store.calls[0][1]


def test_a_key_with_awkward_characters_survives_the_trip(store):
    rpc.put("out/a b+c.txt", b"x")

    assert "%20" in store.calls[0][1]
    assert store.blobs["out/a b+c.txt"] == b"x"


def test_the_worker_token_goes_with_every_call(store):
    rpc.put("k", b"x")
    rpc.get("k")

    assert {c[2] for c in store.calls} == {"Bearer dev-worker"}


def test_what_was_put_comes_back(store):
    rpc.put("k", b"round trip")

    assert rpc.get("k") == b"round trip"


def test_a_download_lands_on_disk(store, tmp_path):
    rpc.put("k", b"downloaded")
    out = tmp_path / "here.bin"

    assert rpc.download("k", out) == out
    assert out.read_bytes() == b"downloaded"
