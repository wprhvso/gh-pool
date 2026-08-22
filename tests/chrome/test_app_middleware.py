from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request, UploadFile
from starlette.testclient import TestClient

from gh_pool.protocol import trace
from gh_pool.server import pool, storage
from gh_pool.server.app import BindTrace, LimitBody, install_errors
from gh_pool.server.sessions import (
    SessionNotFound,
    SessionUnavailable,
    TooManySessions,
)
from gh_pool.relay.tunnel import TunnelDown

LIMIT = 4096
BOUNDARY = "a-boundary-nobody-else-uses"


def _multipart(payload: bytes) -> tuple[bytes, str]:
    head = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="payload.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    return head + payload + tail, f"multipart/form-data; boundary={BOUNDARY}"


def _streamed(body: bytes) -> Iterator[bytes]:
    for offset in range(0, len(body), 1024):
        yield body[offset : offset + 1024]


class Harness:
    def __init__(self, client: TestClient, handled: list[int]) -> None:
        self.client = client
        self.handled = handled


@pytest.fixture
def harness():
    handled: list[int] = []
    app = FastAPI()
    app.add_middleware(LimitBody, limit=LIMIT)
    app.add_middleware(BindTrace)
    install_errors(app)

    async def files(file: UploadFile) -> dict[str, int]:
        content = await file.read()
        handled.append(len(content))
        return {"size": len(content)}

    async def body(request: Request) -> dict[str, int]:
        content = await request.body()
        handled.append(len(content))
        return {"size": len(content)}

    async def seen() -> dict[str, str | None]:
        context = trace.current()
        return {"trace_id": None if context is None else context.trace_id}

    async def failing(name: str) -> None:
        raise {
            "not_found": SessionNotFound("no such session"),
            "unavailable": SessionUnavailable("session is closed"),
            "too_many": TooManySessions("one at a time"),
            "bad_name": storage.BadName(".."),
            "too_large": storage.TooLarge("more than that"),
            "dispatch": pool.DispatchError("the pool is unreachable"),
            "tunnel": TunnelDown("no desktop"),
        }[name]

    app.add_api_route("/files", files, methods=["POST"])
    app.add_api_route("/body", body, methods=["POST"])
    app.add_api_route("/trace", seen, methods=["GET"])
    app.add_api_route("/raise/{name}", failing, methods=["GET"])

    with TestClient(app) as started:
        yield Harness(started, handled)


def test_a_declared_length_over_the_limit_is_refused_before_the_handler(
    harness: Harness,
):
    body, content_type = _multipart(b"x" * (LIMIT * 2))

    answer = harness.client.post(
        "/files", content=body, headers={"content-type": content_type}
    )

    assert answer.status_code == 413
    assert harness.handled == []


def test_a_body_that_declares_no_length_is_counted_as_it_arrives(harness: Harness):
    body, content_type = _multipart(b"x" * (LIMIT * 2))

    answer = harness.client.post(
        "/files",
        content=_streamed(body),
        headers={"content-type": content_type},
    )

    assert answer.status_code == 413
    assert harness.handled == []


def test_a_streamed_body_within_the_limit_still_reaches_the_handler(
    harness: Harness,
):
    payload = b"y" * 512
    body, content_type = _multipart(payload)

    answer = harness.client.post(
        "/files",
        content=_streamed(body),
        headers={"content-type": content_type},
    )

    assert answer.status_code == 200
    assert answer.json() == {"size": len(payload)}


def test_a_streamed_raw_body_over_the_limit_is_refused(harness: Harness):
    answer = harness.client.post("/body", content=_streamed(b"z" * (LIMIT * 3)))

    assert answer.status_code == 413
    assert harness.handled == []


def test_a_body_exactly_on_the_limit_is_allowed(harness: Harness):
    answer = harness.client.post("/body", content=_streamed(b"z" * LIMIT))

    assert answer.status_code == 200
    assert answer.json() == {"size": LIMIT}


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("not_found", 404),
        ("unavailable", 409),
        ("too_many", 429),
        ("bad_name", 400),
        ("too_large", 413),
        ("dispatch", 502),
        ("tunnel", 503),
    ],
)
def test_a_domain_failure_is_answered_with_the_status_it_means(
    harness: Harness, name: str, code: int
):
    answer = harness.client.get(f"/raise/{name}")

    assert answer.status_code == code
    assert answer.json()["detail"]


def test_the_callers_trace_is_bound_for_the_length_of_the_request(harness: Harness):
    parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    answer = harness.client.get("/trace", headers={"traceparent": parent})

    assert answer.json() == {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}


def test_a_request_without_a_trace_is_bound_to_none(harness: Harness):
    assert harness.client.get("/trace").json() == {"trace_id": None}
