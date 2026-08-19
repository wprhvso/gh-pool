from __future__ import annotations

import logging
import threading
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any

from pool_runners.budget import REST
from pool_runners.config import (
    API_VERSION,
    CAPACITY_HEADER,
    POLL_TIMEOUT,
    RUNNER_NAME_PREFIX,
    SESSION_CONFLICT_TRIES,
    SESSION_CONFLICT_WAIT,
    TOKEN_SKEW,
    api_base,
    server_url,
)
from pool_runners.errors import HttpError, RunnerError
from pool_runners.gh import registration_token
from pool_runners.http import jwt_expiry, pause, request
from pool_runners.models import Session, Stats

if TYPE_CHECKING:
    from pool_runners.config import Target

log = logging.getLogger("runners")

_RUNNER_GROUP = 1
_AGENTS = "_apis/distributedtask/pools/0/agents"
_UNAUTHORIZED = 401
_NOT_FOUND = 404
_CONFLICT = 409


class ScaleSet:
    def __init__(self, target: Target) -> None:
        self.target: Target = target
        self._pipeline_url: str | None = None
        self._token: str = ""
        self._token_exp: float = 0.0
        self._lock: threading.Lock = threading.Lock()

    def _authenticate(self, *, force: bool = False) -> None:
        with self._lock:
            if not force and self._token and time.time() < self._token_exp - TOKEN_SKEW:
                return
            info = request(
                "POST",
                f"{api_base()}/actions/runner-registration",
                auth=f"RemoteAuth {registration_token(self.target)}",
                body={
                    "url": f"{server_url()}/{self.target.slug}",
                    "runner_event": "register",
                },
                budget=REST,
            )
            if not isinstance(info, dict) or "token" not in info or "url" not in info:
                raise RunnerError("runner-registration не вернул token/url")
            self._token = str(info["token"])
            self._token_exp = jwt_expiry(self._token)
            self._pipeline_url = str(info["url"]).rstrip("/")
            log.debug(
                "%s: получен pipeline JWT, годен ещё %.0f мин",
                self.target.slug,
                (self._token_exp - time.time()) / 60,
            )

    @property
    def token(self) -> str:
        if not self._token or time.time() > self._token_exp - TOKEN_SKEW:
            self._authenticate()
        return self._token

    @property
    def pipeline_url(self) -> str:
        if self._pipeline_url is None:
            self._authenticate()
        if self._pipeline_url is None:
            raise RunnerError("не удалось получить pipeline URL")
        return self._pipeline_url

    def call(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        auth: str | None = None,
        relative: bool = True,
        **kw: Any,
    ) -> Any:
        url = (
            f"{self.pipeline_url}/_apis/runtime/{path}"
            if relative
            else f"{self.pipeline_url}/{path}"
        )
        url += ("&" if "?" in url else "?") + f"api-version={API_VERSION}"
        if auth is not None:
            return request(method, url, auth=auth, body=body, **kw)
        try:
            return request(method, url, auth=f"Bearer {self.token}", body=body, **kw)
        except HttpError as exc:
            if exc.status != _UNAUTHORIZED:
                raise
            log.info("%s: pipeline JWT отвергнут, переполучаю", self.target.slug)
            self._authenticate(force=True)
            return request(method, url, auth=f"Bearer {self.token}", body=body, **kw)

    def find(self, name: str) -> dict[str, Any] | None:
        query = urllib.parse.quote(name, safe="")
        try:
            found = self.call("GET", f"runnerscalesets?name={query}")
        except HttpError as exc:
            if exc.status == _NOT_FOUND:
                return None
            raise
        values = found.get("value", []) if isinstance(found, dict) else (found or [])
        for item in values:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return None

    def ensure(self, name: str) -> tuple[dict[str, Any], bool]:
        existing = self.find(name)
        if existing:
            log.info(
                "%s: scale set %r уже есть, id=%s",
                self.target.slug,
                name,
                existing["id"],
            )
            return existing, False

        created = self.call(
            "POST",
            "runnerscalesets",
            body={
                "name": name,
                "runnerGroupId": _RUNNER_GROUP,
                "labels": [{"name": name, "type": "System"}],
                "runnerSetting": {"ephemeral": True, "disableUpdate": True},
            },
        )
        if not isinstance(created, dict) or "id" not in created:
            raise RunnerError(f"не удалось создать scale set {name!r}")
        log.info(
            "%s: создал scale set %r, id=%s", self.target.slug, name, created["id"]
        )
        return created, True

    def drop(self, scale_set_id: int) -> None:
        try:
            self.call("DELETE", f"runnerscalesets/{scale_set_id}")
        except HttpError as exc:
            if exc.status != _NOT_FOUND:
                raise
            log.debug("scale set %s уже удалён", scale_set_id)

    def jit(self, scale_set_id: int) -> tuple[int, str, str]:
        name = f"{RUNNER_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
        raw = self.call(
            "POST",
            f"runnerscalesets/{scale_set_id}/generatejitconfig",
            body={"name": name, "workFolder": self.target.work},
        )
        answer = raw if isinstance(raw, dict) else {}
        config = answer.get("encodedJITConfig")
        if not config:
            raise RunnerError("пустой JIT-конфиг")
        runner = answer.get("runner")
        made = runner.get("id") if isinstance(runner, dict) else 0
        return int(made or 0), name, str(config)

    def forget(self, runner_id: int) -> bool:
        if not runner_id:
            return False
        try:
            self.call("DELETE", f"{_AGENTS}/{runner_id}", relative=False)
        except RunnerError as exc:
            log.debug("регистрация %s не снялась: %s", runner_id, exc)
            return False
        return True

    def statistics(self, scale_set_id: int) -> Stats:
        raw = self.call("GET", f"runnerscalesets/{scale_set_id}")
        payload = raw if isinstance(raw, dict) else {}
        return Stats.parse(payload.get("statistics"))

    @staticmethod
    def _session(raw: object) -> Session:
        needed = ("sessionId", "messageQueueUrl", "messageQueueAccessToken")
        if not isinstance(raw, dict) or any(key not in raw for key in needed):
            raise RunnerError("в ответе нет message session")
        token = str(raw["messageQueueAccessToken"])
        return Session(
            session_id=str(raw["sessionId"]),
            queue_url=str(raw["messageQueueUrl"]),
            queue_token=token,
            queue_token_exp=jwt_expiry(token),
            stats=Stats.parse(raw.get("statistics")),
        )

    def open(self, scale_set_id: int, owner: str) -> Session:
        session = self._session(
            self.call(
                "POST",
                f"runnerscalesets/{scale_set_id}/sessions",
                body={"ownerName": owner},
            )
        )
        log.info("%s: открыл message session %s", self.target.slug, session.session_id)
        return session

    def refresh(self, scale_set_id: int, session: Session) -> Session:
        raw = self.call(
            "PATCH", f"runnerscalesets/{scale_set_id}/sessions/{session.session_id}"
        )
        log.info("%s: обновил токен очереди", self.target.slug)
        return self._session(raw)

    def close(self, scale_set_id: int, session: Session) -> None:
        try:
            self.call(
                "DELETE",
                f"runnerscalesets/{scale_set_id}/sessions/{session.session_id}",
            )
        except HttpError as exc:
            if exc.status != _NOT_FOUND:
                raise
            log.debug("сессия %s уже закрыта", session.session_id)
            return
        log.info("%s: закрыл message session %s", self.target.slug, session.session_id)

    def reopen(self, scale_set_id: int, session: Session | None, owner: str) -> Session:
        if session is not None:
            try:
                self.close(scale_set_id, session)
            except RunnerError as exc:
                log.debug("старая сессия не закрылась: %s", exc)
        for attempt in range(SESSION_CONFLICT_TRIES):
            try:
                return self.open(scale_set_id, owner)
            except HttpError as exc:
                if exc.status != _CONFLICT or attempt + 1 >= SESSION_CONFLICT_TRIES:
                    raise
                log.warning(
                    "%s: на scale set висит чужая сессия, жду %.0f с (%s/%s)",
                    self.target.slug,
                    SESSION_CONFLICT_WAIT,
                    attempt + 1,
                    SESSION_CONFLICT_TRIES,
                )
                if pause(SESSION_CONFLICT_WAIT):
                    raise
        raise RunnerError("сессия так и не открылась")

    def poll(
        self, session: Session, last_message_id: int = 0, capacity: int = 0
    ) -> dict[str, Any] | None:
        url = session.queue_url
        if last_message_id > 0:
            url += ("&" if "?" in url else "?") + f"lastMessageId={last_message_id}"
        message = request(
            "GET",
            url,
            auth=f"Bearer {session.queue_token}",
            timeout=POLL_TIMEOUT,
            attempts=1,
            extra={
                "Accept": f"application/json; api-version={API_VERSION}",
                CAPACITY_HEADER: str(capacity),
            },
        )
        return message if isinstance(message, dict) else None

    def ack(self, session: Session, message_id: int) -> None:
        base, _, query = session.queue_url.partition("?")
        request(
            "DELETE",
            f"{base}/{message_id}?{query}" if query else f"{base}/{message_id}",
            auth=f"Bearer {session.queue_token}",
            attempts=2,
        )

    def acquirable(self, scale_set_id: int) -> list[dict[str, Any]]:
        raw = self.call("GET", f"runnerscalesets/{scale_set_id}/acquirablejobs")
        if not raw:
            return []
        return raw.get("value", []) if isinstance(raw, dict) else raw

    def acquire(
        self, scale_set_id: int, session: Session, request_ids: list[int]
    ) -> int:
        if not request_ids:
            return 0
        raw = self.call(
            "POST",
            f"runnerscalesets/{scale_set_id}/acquirejobs",
            body=request_ids,
            auth=f"Bearer {session.queue_token}",
        )
        taken = raw.get("value", []) if isinstance(raw, dict) else (raw or [])
        return len(taken) if isinstance(taken, list) else 0
