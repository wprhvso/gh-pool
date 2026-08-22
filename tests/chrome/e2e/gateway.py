import asyncio

from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _claims(app: ASGIApp, scope: Scope) -> bool:
    routes = getattr(app, "routes", ())
    return any(route.matches(scope)[0] is not Match.NONE for route in routes)


class Gateway:
    def __init__(self, server: ASGIApp, relay: ASGIApp) -> None:
        self._server = server
        self._relay = relay

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        await self._pick(scope)(scope, receive, send)

    def _pick(self, scope: Scope) -> ASGIApp:
        if _claims(self._server, scope):
            return self._server
        return self._relay if _claims(self._relay, scope) else self._server

    async def _lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        apps = (self._server, self._relay)
        inboxes: list[asyncio.Queue[Message]] = [asyncio.Queue() for _ in apps]
        outbox: asyncio.Queue[Message] = asyncio.Queue()
        running = [
            asyncio.create_task(app(dict(scope), inbox.get, outbox.put))
            for app, inbox in zip(apps, inboxes, strict=True)
        ]
        try:
            while True:
                message = await receive()
                for inbox in inboxes:
                    inbox.put_nowait(message)
                replies = [await outbox.get() for _ in apps]
                broken = next(
                    (r for r in replies if r["type"].endswith(".failed")), None
                )
                await send(broken or replies[0])
                if message["type"] == "lifespan.shutdown":
                    return
        finally:
            for task in running:
                task.cancel()
