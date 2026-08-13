# gh-chrome

Chrome on a GitHub Actions runner, driven from your machine over HTTPS, with the
session recorded and seekable in a browser.

```
your code ──POST──▶ server (VPS) ──SSE──▶ runner (GitHub Actions)
     ▲                  │  postgres            │  Xvnc + openbox + Chrome
     └────── SSE ───────┘  segments            │  XTEST input
                           profiles            └──▶ ffmpeg ──POST──▶ server

your browser ──HTTPS──▶ server ◀─one websocket─ runner ──▶ KasmVNC on 127.0.0.1
```

The control plane is server-sent events downstream and POST upstream. `new()`
returns a session id at once; the server dispatches the workflow and the runner
connects back with that id. Commands go into a strictly sequential queue and
return a handle you await. Input goes through XTEST on a real cursor, not
through CDP.

## Quick start

```bash
export GH_CHROME_URL=https://chrome.example.com
export GH_CHROME_TOKEN=...
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
| `GH_CHROME_VNC` | `0` on the runner skips the desktop |
| `GH_CHROME_VNC_PORT` | port KasmVNC listens on, loopback only |
| `GH_CHROME_VNC_FRAME_RATE` | updates per second, until a viewer sets its own |

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
hooks are in place before any application code runs; `install()` also seeds the
document that is already open. Three actions: `fulfill` answers without touching
the network, `rewrite` swaps the request body, `capture` records url, method,
headers and body and answers with `status` instead of letting it out.

## Server

| Variable | Meaning |
| --- | --- |
| `GH_CHROME_TOKEN` | shared secret for the API and the player |
| `GH_CHROME_DATABASE_URL` | libpq connection string |
| `GH_CHROME_STORAGE` | directory for recordings, profiles and uploads |
| `GH_CHROME_PUBLIC_URL` | origin the runner connects back to |
| `GH_CHROME_GITHUB_REPO` | repository the browser workflow lives in |
| `GH_CHROME_GITHUB_PAT` | token with the `actions:write` scope |

Run `gh-chrome-server`. In the repository that hosts the workflow add
`GH_CHROME_URL` and `GH_CHROME_TOKEN` as secrets; `GH_CHROME_PROXY` sends the
runner's traffic through a proxy of your own.

On NixOS the flake ships the server as a module:

```nix
{
  inputs.gh-chrome.url = "github:wprhvso/gh-chrome";

  modules = [ inputs.gh-chrome.nixosModules.default ];
}
```

```nix
{
  services.gh-chrome = {
    enable = true;
    port = 8001;
    publicUrl = "https://chrome.example.com";
    database.createLocally = true;
    environmentFiles = [ "/var/lib/secrets/gh-chrome" ];
  };
}
```

The token, the PAT and the database URL stay in the environment file.

## Packaging

The base distribution installs what the client needs and nothing else: `httpx`
and `pydantic`. Extra `server` adds FastAPI, psycopg and uvicorn; extra `runner`
adds websockets and python-xlib.

## Development

```bash
nix develop
uv sync --all-extras
```

```bash
gh-chrome-runner --session <id> --server http://127.0.0.1:8000
```

KasmVNC is not in nixpkgs, so the dev shell has no `Xkasmvnc` and the runner
falls back to Xvfb; `x11vnc -display :99 -nopw -forever` is enough to watch it.
Install `kasmvncserver` from the KasmVNC releases to get the live desktop
locally.

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
