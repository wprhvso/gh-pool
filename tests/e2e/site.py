import asyncio
from collections import Counter
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route
from tests.e2e.stack import Background

ASSET = b"a byte or two to download" * 40

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;font:16px sans-serif">{body}</body></html>
"""

TRACE = """
window.trace = [];
const record = (event) => window.trace.push({
  type: event.type,
  trusted: event.isTrusted,
  target: event.target.id || event.target.tagName,
  x: event.clientX ?? null,
  y: event.clientY ?? null,
  key: event.key ?? null,
  ctrl: Boolean(event.ctrlKey),
  detail: event.detail ?? 0,
});
for (const type of ['click', 'dblclick', 'contextmenu', 'mousemove', 'keydown', 'wheel', 'change'])
  document.addEventListener(type, record, true);
"""

FORM = PAGE.format(
    title="form",
    body="""
<h1 id="heading">Form</h1>
<input id="name" style="position:absolute;left:40px;top:120px;width:320px;height:36px">
<textarea id="notes" style="position:absolute;left:40px;top:180px;width:320px;height:80px"></textarea>
<select id="colour" style="position:absolute;left:40px;top:290px;width:200px;height:32px">
  <option value="red">Red</option><option value="blue">Blue</option>
</select>
<button id="save" style="position:absolute;left:40px;top:350px;width:220px;height:52px">Save</button>
<div id="saved" style="position:absolute;left:40px;top:420px;display:none">saved</div>
<div id="menu" style="position:absolute;left:40px;top:460px;display:none">menu</div>
<script>
"""
    + TRACE
    + """
document.getElementById('save').addEventListener('click', () => {
  document.getElementById('saved').style.display = 'block';
});
document.addEventListener('contextmenu', (event) => {
  event.preventDefault();
  document.getElementById('menu').style.display = 'block';
});
</script>
""",
)

SLOW = PAGE.format(
    title="slow",
    body="""
<div id="early">early</div>
<div id="doomed">doomed</div>
<script>
const wait = Number(new URLSearchParams(location.search).get('ms') || '600');
setTimeout(() => {
  const late = document.createElement('div');
  late.id = 'late';
  late.textContent = 'late';
  document.body.appendChild(late);
  document.getElementById('doomed').remove();
  window.arrived = true;
}, wait);
</script>
""",
)

TALL = PAGE.format(
    title="tall",
    body="""
<div id="offset" style="position:fixed;left:0;top:0;background:#eee">0</div>
<div style="height:1200px">top</div>
<button id="deep" style="width:200px;height:60px">deep</button>
<div style="height:2400px">filler</div>
<div id="bottom">bottom</div>
<script>
"""
    + TRACE
    + """
window.addEventListener('scroll', () => {
  document.getElementById('offset').textContent = String(Math.round(window.scrollY));
});
</script>
""",
)

OVERLAY = PAGE.format(
    title="overlay",
    body="""
<button id="covered" style="position:absolute;left:60px;top:120px;width:200px;height:60px">covered</button>
<div id="veil" style="position:fixed;inset:0;background:rgba(0,0,0,0.05);z-index:9"></div>
""",
)

LINKS = PAGE.format(
    title="links",
    body="""
<a id="external" href="/title/opened" target="_blank"
   style="position:absolute;left:40px;top:120px">open a tab</a>
<button id="popup" style="position:absolute;left:40px;top:180px;width:200px;height:48px"
        onclick="window.open('/title/popped', '_blank')">popup</button>
""",
)

DOWNLOAD = PAGE.format(
    title="download",
    body="""
<a id="grab" href="/asset/report.bin" download
   style="position:absolute;left:40px;top:120px">grab it</a>
<a id="sneaky" href="/asset/report.bin?as=a%20report%20%231%3Fv%3D2.bin" download
   style="position:absolute;left:40px;top:190px">grab it under another name</a>
""",
)

UPLOAD = PAGE.format(
    title="upload",
    body="""
<input type="file" id="pick" style="position:absolute;left:40px;top:120px">
<pre id="content"></pre>
<script>
document.getElementById('pick').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  document.getElementById('content').textContent = file.name + ':' + (await file.text());
});
</script>
""",
)

NET = PAGE.format(
    title="net",
    body="""
<button id="send" style="position:absolute;left:40px;top:120px;width:200px;height:48px">send</button>
<button id="xhr" style="position:absolute;left:40px;top:190px;width:200px;height:48px">xhr</button>
<button id="stream" style="position:absolute;left:40px;top:260px;width:200px;height:48px">stream</button>
<button id="slow" style="position:absolute;left:40px;top:330px;width:200px;height:48px">slow stream</button>
<button id="plain" style="position:absolute;left:40px;top:400px;width:200px;height:48px">plain xhr</button>
<button id="again" style="position:absolute;left:40px;top:470px;width:200px;height:48px">reuse xhr</button>
<pre id="answer"></pre>
<pre id="headers"></pre>
<script>
const answer = document.getElementById('answer');
const headers = document.getElementById('headers');
document.getElementById('stream').addEventListener('click', async () => {
  const response = await fetch('/api/stream');
  answer.textContent = response.status + ':' + (await response.text());
});
document.getElementById('slow').addEventListener('click', async () => {
  const response = await fetch('/api/stream?chunks=20&delay=0.2');
  answer.textContent = response.status + ':' + (await response.text());
});
document.getElementById('send').addEventListener('click', async () => {
  const response = await fetch('/api/echo', {
    method: 'POST',
    headers: {'content-type': 'application/json', 'x-marker': 'from-the-page'},
    body: JSON.stringify({said: 'hello'}),
  });
  headers.textContent = response.headers.get('content-type');
  answer.textContent = response.status + ':' + (await response.text());
});
document.getElementById('xhr').addEventListener('click', () => {
  const request = new XMLHttpRequest();
  request.open('POST', '/api/echo');
  request.setRequestHeader('content-type', 'application/json');
  request.onload = () => {
    headers.textContent = request.getResponseHeader('content-type') + '|' +
      (request.getResponseHeader('x-canned') || '-');
    answer.textContent = request.status + ':' + request.responseText;
  };
  request.send(JSON.stringify({said: 'by xhr'}));
});
// No content type of its own: the one on the wire is the one XHR derives from
// the body, and a capture that leaves it out replays as a different request.
document.getElementById('plain').addEventListener('click', () => {
  const request = new XMLHttpRequest();
  request.open('POST', '/api/echo');
  request.onload = () => {
    answer.textContent = request.status + ':' + request.responseText;
  };
  request.send('said=by a plain xhr');
});
// One object, two requests: the second is for an address no rule matches, so
// what it shows has to be the site's own answer.
document.getElementById('again').addEventListener('click', () => {
  const request = new XMLHttpRequest();
  request.open('POST', '/api/echo');
  request.onload = () => {
    request.open('POST', '/api/elsewhere');
    request.onload = () => {
      answer.textContent = request.status + ':' + request.responseText;
    };
    request.send('the second one');
  };
  request.send('the first one');
});
</script>
""",
)

BUSY = PAGE.format(
    title="busy",
    body="""
<div id="early">early</div>
<script>
// Started while the document is parsing and finished well after load, so the
// difference between waiting for the load event and waiting for the network to
// go quiet is something a test can see.
window.settled = false;
fetch('/api/stream?chunks=4&delay=0.4').then((response) => response.text()).then(() => {
  window.settled = true;
});
</script>
""",
)

STATE = PAGE.format(
    title="state",
    body="""
<div id="visits">0</div>
<div id="cookie"></div>
<script>
const visits = Number(localStorage.getItem('visits') || '0') + 1;
localStorage.setItem('visits', String(visits));
document.getElementById('visits').textContent = String(visits);
document.getElementById('cookie').textContent = document.cookie;
</script>
""",
)


class Site:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.posted: list[dict[str, str]] = []
        self.finished = 0
        self._background = Background(self._build())

    def start(self) -> None:
        self._background.start()

    def stop(self) -> None:
        self._background.stop()

    def reset(self) -> None:
        self.hits.clear()
        self.posted.clear()
        self.finished = 0

    def url(self, path: str = "/") -> str:
        return f"{self._background.url}{path}"

    def _build(self) -> Starlette:
        return Starlette(
            routes=[
                Route(
                    "/",
                    self._page(
                        PAGE.format(title="index", body="<h1 id='heading'>index</h1>")
                    ),
                ),
                Route("/form", self._page(FORM)),
                Route("/slow", self._page(SLOW)),
                Route("/tall", self._page(TALL)),
                Route("/overlay", self._page(OVERLAY)),
                Route("/links", self._page(LINKS)),
                Route("/busy", self._page(BUSY)),
                Route("/download", self._page(DOWNLOAD)),
                Route("/upload", self._page(UPLOAD)),
                Route("/net", self._page(NET)),
                Route("/state", self._state),
                Route("/title/{text}", self._title),
                Route("/redirect", self._redirect),
                Route("/asset/{name}", self._asset),
                Route("/api/echo", self._echo, methods=["POST"]),
                Route("/api/elsewhere", self._echo, methods=["POST"]),
                Route("/api/stream", self._stream, methods=["GET", "POST"]),
            ]
        )

    def _page(self, html: str):
        async def render(request: Request) -> Response:
            self.hits[request.url.path] += 1
            return HTMLResponse(html)

        return render

    async def _state(self, request: Request) -> Response:
        self.hits[request.url.path] += 1
        response = HTMLResponse(STATE)
        response.set_cookie("visitor", "the-same-browser", max_age=3600, path="/")
        return response

    async def _title(self, request: Request) -> Response:
        self.hits[request.url.path] += 1
        text = str(request.path_params["text"])
        return HTMLResponse(PAGE.format(title=text, body=f"<h1 id='what'>{text}</h1>"))

    async def _redirect(self, request: Request) -> Response:
        self.hits[request.url.path] += 1
        return RedirectResponse("/title/redirected")

    async def _asset(self, request: Request) -> Response:
        self.hits[request.url.path] += 1
        name = request.query_params.get("as") or str(request.path_params["name"])
        return Response(
            ASSET,
            media_type="application/octet-stream",
            headers={"content-disposition": f'attachment; filename="{name}"'},
        )

    async def _echo(self, request: Request) -> Response:
        self.hits[request.url.path] += 1
        body = (await request.body()).decode()
        self.posted.append(
            {"body": body, "marker": request.headers.get("x-marker", "")}
        )
        return JSONResponse({"echoed": body, "seen": self.hits[request.url.path]})

    async def _stream(self, request: Request) -> Response:
        self.hits[request.url.path] += 1
        count = int(request.query_params.get("chunks", "4"))
        delay = float(request.query_params.get("delay", "0.05"))

        async def chunks() -> AsyncIterator[bytes]:
            for index in range(count):
                yield f"chunk-{index};".encode()
                await asyncio.sleep(delay)
            self.finished += 1

        return StreamingResponse(chunks(), media_type="text/plain")
