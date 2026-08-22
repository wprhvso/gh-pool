from __future__ import annotations

from pool_runners.errors import HttpError, RateLimited
from pool_runners.redact import redact


def test_plain_text_is_left_alone() -> None:
    assert redact("всё хорошо") == "всё хорошо"


def test_session_id_is_hidden() -> None:
    assert redact("?sessionId=DEAD-beef-01") == "?sessionId=<session>"


def test_opaque_pipeline_segment_is_hidden() -> None:
    hidden = redact("https://x.pipelines.actions.githubusercontent.com/SECRET/q/1")
    assert "SECRET" not in hidden
    assert hidden.endswith("<opaque>/q/1")


def test_query_tokens_are_hidden() -> None:
    assert redact("?token=abc&x=1") == "?token=<token>&x=1"
    assert redact("?access_token=abc") == "?access_token=<token>"


def test_everything_at_once() -> None:
    hidden = redact(
        "https://x.pipelines.actions.githubusercontent.com/SECRET/q"
        "?sessionId=1a2b&access_token=zzz"
    )
    assert "SECRET" not in hidden
    assert "1a2b" not in hidden
    assert "zzz" not in hidden


def test_errors_carry_redacted_text() -> None:
    error = HttpError(404, "https://q/x?token=zzz", "нет такого token=zzz")
    assert "zzz" not in str(error)
    assert error.status == 404


def test_a_long_body_is_cut() -> None:
    error = HttpError(500, "https://q/x", "я" * 5000)
    assert len(error.body) == 500


def test_rate_limited_says_how_long_to_wait() -> None:
    error = RateLimited(429, "https://q/x", "перебор", 42.4)
    assert "42" in str(error)
    assert error.retry_in == 42.4
