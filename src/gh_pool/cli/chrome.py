import webbrowser
from urllib.parse import urlencode

USER = "admin"

# The KasmVNC client resolves its websocket against the origin root, so a page
# served under the session prefix has to be told where the socket actually is.
# These are the settings the player hands its own "open on its own" link.
CLIENT = (
    ("resize", "scale"),
    ("reconnect", "true"),
    ("reconnect_delay", "2000"),
    ("reconnect_retries", "1000"),
)


def player(server: str, session_id: str) -> str:
    return f"{server}/s/{session_id}"


def desktop(server: str, session_id: str) -> str:
    query = urlencode([("path", f"s/{session_id}/vnc/websockify"), *CLIENT])
    return f"{player(server, session_id)}/vnc/?{query}"


def open_new(url: str) -> bool:
    try:
        return webbrowser.open_new(url)
    except Exception:
        return False
