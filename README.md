# gh-chrome

Chrome on a GitHub Actions runner, driven from your machine over HTTPS, with the
session recorded and seekable in a browser.

```
your code ──POST──▶ server (VPS) ──SSE──▶ runner (GitHub Actions)
     ▲                  │  postgres            │  Xvfb + openbox + Chrome
     └────── SSE ───────┘  segments            │  XTEST input
                           profiles            └──▶ ffmpeg ──POST──▶ server
```

Server-sent events downstream, POST upstream, no websockets. `new()` returns a
session id at once; the server dispatches the workflow and the runner connects
back with that id. Commands go into a strictly sequential queue and return a
handle you await. Input goes through XTEST on a real cursor, not through CDP.

## Quick start

```bash
export GH_CHROME_URL=https://chrome.example.com
export GH_CHROME_TOKEN=...
python examples/hello.py
```

The player lives at `https://chrome.example.com/s/<id>`: user `admin`, password
the token.

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
gh-chrome-runner --session <id> --server http://127.0.0.1:8000 &
x11vnc -display :99 -nopw -forever
```

## Limits

| Limit | Consequence |
| --- | --- |
| six hours per job | the runner dies, the profile is flagged stale |
| one token | no separation between users or sessions |
| a fresh Azure IP per run | saved cookies still get re-verified |
| the profile is archived at a clean shutdown | a killed job loses the session |
| DASH-LL, one to three seconds behind | a recording to scrub, not a desktop |

GitHub Actions is meant for CI; this is a demonstration, not a way to dodge
compute costs.

## License

MIT
