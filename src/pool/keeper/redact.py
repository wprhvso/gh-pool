from __future__ import annotations

import re

_SESSION = re.compile(r"(sessionId=)[0-9a-f-]+", re.IGNORECASE)
_OPAQUE = re.compile(
    r"(pipelines\.actions\.githubusercontent\.com/)[^/]+", re.IGNORECASE
)
_QUERY_TOKEN = re.compile(r"((?:access_)?token=)[^&\s]+", re.IGNORECASE)


def redact(text: str) -> str:
    text = _OPAQUE.sub(r"\1<opaque>", text)
    text = _SESSION.sub(r"\1<session>", text)
    return _QUERY_TOKEN.sub(r"\1<token>", text)
