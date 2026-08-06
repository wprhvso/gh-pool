# gh-chrome

Chrome, running on a GitHub Actions runner, driven from your machine over HTTPS,
with the whole session recorded and seekable in a browser.

This is a proof of concept. It exists to show the thing is possible, not to be a
substitute for a browser you own.

## How it works

```
your code ──POST──▶ server (VPS) ──SSE──▶ runner (GitHub Actions)
     ▲                  │  postgres            │  Xvfb + openbox + Chrome
     └────── SSE ───────┘  segments            │  XTEST input
                           profiles            └──▶ ffmpeg ──POST──▶ server
```

Everything is plain HTTPS: server-sent events downstream, POST upstream. No
websockets anywhere in the protocol.

Calling `new()` returns a session id immediately; the server dispatches the
workflow in the background and the runner connects back with that id. Commands
go into a strictly sequential queue and return a handle you await. Input is not
CDP — the runner moves a real cursor along a WindMouse trajectory and types
through XTEST, so the page sees trusted events.

## Quick start

```bash
export GH_CHROME_URL=https://chrome.example.com
export GH_CHROME_TOKEN=...
python examples/hello.py
```

Watch the session at `https://chrome.example.com/s/<id>` (user `admin`, password
is the token).

## Deploying the server

Set `GH_CHROME_TOKEN`, `GH_CHROME_DATABASE_URL`, `GH_CHROME_STORAGE`,
`GH_CHROME_PUBLIC_URL`, `GH_CHROME_GITHUB_REPO` and `GH_CHROME_GITHUB_PAT`
(needs the `actions:write` scope), then run `gh-chrome-server`.

In the repository that runs the workflow, add `GH_CHROME_URL` and
`GH_CHROME_TOKEN` as secrets.

## Development

```bash
nix develop
uv sync
uv run pytest -q
```

Tests that need a display are skipped when `Xvfb` is missing or
`GH_CHROME_SKIP_X` is set. To watch the runner locally:

```bash
gh-chrome-runner --session <id> --server http://127.0.0.1:8000 &
x11vnc -display :99 -nopw -forever
```

## What this cannot do

**Six hours per session.** That is the GitHub job limit. When it expires the
runner dies, the profile is not saved, and the profile is flagged stale.

**One token, no isolation.** Anyone holding the token can read and control any
session. There is no per-user separation by design.

**A new IP every time.** Each runner comes up in a random Azure range. Cookies
survive in the profile archive, but fraud detection keys on the address, so
Google and banks will re-verify no matter how good the saved state is. Route
outbound traffic through your own VPS (`GH_CHROME_PROXY`) if you need a session
to actually hold.

**No state after a crash.** The profile is archived once, at a clean shutdown.
A killed job loses everything since the session started.

**Latency.** DASH-LL puts the recording one to three seconds behind reality.
It is a recording you can scrub, not a remote desktop.

## Terms

GitHub Actions is meant for CI. This project runs a browser there, which is
outside what the Actions terms contemplate — it is a demonstration, and running
it at scale, or to dodge compute costs, is not something the terms allow.

## License

MIT
