# gh-chrome

Chrome on a GitHub Actions runner, driven from your machine over HTTPS, with the
session recorded and seekable in a browser.

```
your code ──POST──▶ server (VPS) ──SSE──▶ runner (a pool worker)
     ▲                  │  postgres            │  Xvnc + openbox + Chrome
     └────── SSE ───────┘  segments            │  XTEST input
                           profiles            └──▶ ffmpeg ──POST──▶ server

your browser ──HTTPS──▶ server ◀─one websocket─ runner ──▶ KasmVNC on 127.0.0.1
```

The control plane is server-sent events downstream and POST upstream. `new()`
returns a session id at once; the server submits a task to the pool and the
runner, started inside a worker that is already up, connects back with that id. Commands go into a strictly sequential queue and
return a handle you await. Input goes through XTEST on a real cursor, not
through CDP.

## Quick start

```bash
export GH_POOL_URL=https://chrome.example.com
export GH_POOL_TOKEN=...
python examples/hello.py
```

The player lives at `https://chrome.example.com/s/<id>`: user `admin`, password
the token.

## Live desktop

The runner's X display is KasmVNC's own X server, so the screen ffmpeg records
is a desktop you can take over. Open the player and switch to *live desktop*:
real mouse, real keyboard, clipboard both ways, and KasmVNC's own quality
controls. Your commands and your hands drive the same cursor, so a script that
clicks while you type will fight you.

The runner has no inbound address, so it opens one websocket back to the server
and the server multiplexes the browser's requests down it. Nothing new listens
on the runner: KasmVNC binds to loopback with its own authentication off,
because the tunnel is the only way in and the player's credentials guard it.

The desktop sits in a frame, which costs it cross-origin isolation and with it
the client's threaded decoder. *open on its own* puts the same desktop in a tab
of its own, where it is isolated and decodes faster.

Two websockets cross whatever sits in front of the server — the runner's tunnel
and the browser's desktop — so a reverse proxy has to pass an upgrade through
and leave it unbuffered. On nginx that is:

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $connection_upgrade;
proxy_buffering off;
```

Without `proxy_http_version` nginx talks HTTP/1.0 upstream and the upgrade
never arrives at all. Uvicorn pings both sockets every twenty seconds, so an idle
desktop outlives the default read timeout on its own. Serve the whole thing
over HTTPS: the clipboard and the hardware video decoder are only offered to a
secure origin.

| Variable | Meaning |
| --- | --- |
| `GH_POOL_VNC` | `0` on the runner skips the desktop |
| `GH_POOL_VNC_PORT` | port KasmVNC listens on, loopback only |
| `GH_POOL_VNC_FRAME_RATE` | updates per second, until a viewer sets its own |

The workflow installs `kasmvncserver` from the KasmVNC releases and lets that
step fail. Without it the runner falls back to Xvfb: the recording, the commands
and the profile all keep working, and the player reports a desktop that never
connected.

## Network taps

`Tap` patches `fetch` and `XMLHttpRequest` inside the page, so a request the
application makes can be answered locally, sent with a different body, or taken
apart and replayed. The replay runs in the page too: same cookies, same IP, same
TLS fingerprint as everything else the tab does.

```python
tap = Tap(session)
await tap.arm()
await session.goto("https://example.com")
await tap.install([Rule(name="send", url="/api/send", action="capture", status=400)])

await session.click("#send")
captured = await tap.take("send", timeout=30)

async for chunk in tap.replay(captured, body=my_payload, timeout=300):
    print(chunk, end="")
```

`arm()` registers the script for every document the tab loads afterwards, so the
hooks are in place before any application code runs; `install()` seeds the
document that is already open and registers the rules for the ones that follow,
so a reload, a redirect or an iframe is tapped like the first page. Three
actions: `fulfill` answers without touching the network, `rewrite` swaps the
request body, `capture` records url, method, headers and body and answers with
`status` instead of letting it out.

## Tracing

A command is enqueued on one connection and executed on another, opened at
session start and belonging to no request, so a `traceparent` header stops at
the server unless something carries it further. It is stored on the command and
handed to the runner in the envelope, which puts the runner's work under the
span that asked for it rather than in a trace of its own.

Nothing here depends on OpenTelemetry, and the base distribution is still
`httpx` and `pydantic`. The header is validated, kept as it arrived and passed
on; a version this code has never heard of is forwarded unchanged, and a
malformed one is dropped rather than pointed at a parent that does not exist.
Whoever instruments the client's `httpx` — the [ai][ai] gateway does — sends the
header without being asked, so nothing in the client had to change.

The server and the runner both put the trace id in every log line, which is
what lets a request be followed across three processes and two machines:

```
2026-08-15 09:41:02 INFO    [4bf92f3577b34da6a3ce929d0e0e4736] gh_pool.browser.loop: command click failed: timeout
2026-08-15 09:41:02 INFO    [-] gh_pool.browser.capture: segment 143 uploaded
```

A `-` is work that belongs to no request: the recorder, the heartbeat, the
watchdog. The session's event reader is deliberately started from an empty
context — it outlives by hours the call that opened the session, and a task
keeps whatever context it was created in.

[ai]: https://github.com/wprhvso/ai

## Server

| Variable | Meaning |
| --- | --- |
| `GH_POOL_TOKEN` | shared secret for the API and the player |
| `GH_POOL_DATABASE_URL` | libpq connection string |
| `GH_POOL_STORAGE` | directory for recordings, profiles and uploads |
| `GH_POOL_PUBLIC_URL` | origin the runner connects back to |
| `GH_POOL_POOL_SERVER` | base URL of the [pool](https://github.com/wprhvso/gh-pool) |
| `GH_POOL_POOL_TOKEN` | pool client token |
| `GH_POOL_RUNNER_SPEC` | what to install for the runner, defaults to the server's own version |

Run `gh-pool-server`. The runner needs no secrets of its own: the server mints
a token for each session and hands it to the pool task along with the session id.
`GH_POOL_PROXY` sends the runner's traffic through a proxy of your own.

`GET /healthz` is the one route with no credentials on it. It reaches for a
connection and asks postgres a question: 200 while the database answers, 503
once it stops. Nothing else is in the verdict — a server whose pool is
unreachable and whose tunnels are all down still serves the API correctly, and
taking it out of rotation would only make that worse.

## Housekeeping

A recording is about a gigabyte an hour and nothing asked the server to keep it
forever, so a background pass throws old sessions out. It removes
`sessions/<id>` and `files/<id>` and then the row, which takes the commands,
events, downloads and uploads with it. Sessions that are still pending or
active are never candidates, and `profiles/` is not the cleaner's business at
any point: an archive is a Google account somebody signed in by hand, and it
goes only when you ask for it with `DELETE /profiles/{name}`.

Two limits, applied in that order. First everything closed longer than
`CLEANUP_MAX_DAYS` ago. Then, if what the sessions hold is still over
`CLEANUP_MAX_BYTES`, the oldest closed sessions go one at a time until it fits
— all but the ones closed within `GH_POOL_RUNNER_GRACE`, whose runner is
still allowed to hand in a last segment. When there is nothing left to take and
it still does not fit, the pass says so in the log and stops.

| Variable | Meaning |
| --- | --- |
| `GH_POOL_CLEANUP_MAX_DAYS` | how long a closed session is kept, default 7 |
| `GH_POOL_CLEANUP_MAX_BYTES` | what sessions and uploads may hold, default 64 GiB |
| `GH_POOL_CLEANUP_INTERVAL` | seconds between passes, default 3600 |
| `GH_POOL_CLEANUP_DELAY` | seconds before the first pass, default 60 |

## Kubernetes

One replica, `strategy: Recreate`. The tunnels, the queue of cancels and the
set of sessions on their way out live in the process, and the recordings live
on a disk, so a second replica would answer for sessions it cannot see.

Storage is a PVC mounted at `/var/lib/gh-chrome`, which is where the image
already points `GH_POOL_STORAGE`. It holds `sessions/`, `files/` and
`profiles/`; only the last one cannot be regenerated, and it is the one the
cleaner never touches. Size the volume above `GH_POOL_CLEANUP_MAX_BYTES` plus
whatever the live sessions and the profiles need.

`GH_POOL_TOKEN` and `GH_POOL_POOL_TOKEN` belong in a Secret, and
`GH_POOL_DATABASE_URL` too when the password is in it. The rest is a
ConfigMap. `GH_POOL_HOST` is already `0.0.0.0` in the image.

Point both probes at `/healthz`. The root filesystem can be read-only, but the
server still needs a writable `/tmp`: an upload over a megabyte is spooled
there by starlette before it is handed to the storage directory, so give the
pod an `emptyDir` at `/tmp` sized for `GH_POOL_MAX_UPLOAD`.

## Packaging

The base distribution installs what the client needs and nothing else: `httpx`
and `pydantic`. Extra `server` adds FastAPI, psycopg and uvicorn; экстра `browser`
adds websockets and python-xlib.

## Development

```bash
nix develop
uv sync --all-extras
```

```bash
gh-pool-browser --session <id> --server http://127.0.0.1:8000
```

KasmVNC is not in nixpkgs, so the dev shell has no `Xkasmvnc` and the runner
falls back to Xvfb; `x11vnc -display :99 -nopw -forever` is enough to watch it.
Install `kasmvncserver` from the KasmVNC releases to get the live desktop
locally.

## Tests

```bash
uv run pytest -o asyncio_mode=auto -o pythonpath=.
```

The pytest settings live in `wprhvso/qa-python` and are laid down by the action,
so a local run passes the two that matter by hand.

`tests/` holds the unit tests. `tests/e2e/` puts the whole thing together: the
real server on a real port, the real client against it, a website of its own on
loopback, and a runner on the other end of the command stream. It comes in two
tiers, and each one skips itself, with a reason, on a machine that cannot host
it.

| Tier | What it needs | What it drives |
| --- | --- | --- |
| protocol | postgres | sessions, the command queue, events, timeouts, transfers, the desktop tunnel |
| browser (`-m browser`) | an X server, Chrome, ffmpeg, zstd | navigation, the DOM, XTEST input, tabs, uploads, downloads, taps, the recording, profiles |

The protocol tier answers commands from a scripted runner that speaks the
runner's half of the wire; the browser tier starts `gh-pool-browser` itself,
with its own display, its own Chrome and its own recorder, one session per test.

| Variable | Meaning |
| --- | --- |
| `GH_POOL_TEST_DATABASE_URL` | a cluster to test against; without it the suite puts up a throwaway one, which `initdb` will not do for root |
| `GH_POOL_TEST_CHROME` | the browser to drive, when it is not on `PATH` |

`-m "not browser"` leaves the browser tier out; `pytest tests/e2e/test_input.py`
runs one part of it.

## Limits

| Limit | Consequence |
| --- | --- |
| six hours per job | the runner dies, the profile is flagged stale |
| one token | no separation between users or sessions |
| a fresh Azure IP per run | saved cookies still get re-verified |
| the profile is archived at a clean shutdown | a killed job loses the session |
| DASH-LL, one to three seconds behind | scrub the recording, act on the desktop |
| every desktop pixel crosses the runner, the server and your link | latency is the sum of three hops |
| a viewer that stops draining is cut off, not throttled | a link too slow for the desktop reconnects instead of lagging |

GitHub Actions is meant for CI; this is a demonstration, not a way to dodge
compute costs.

## License

MIT
