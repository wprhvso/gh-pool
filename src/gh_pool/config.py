"""Переходный слой имён переменных окружения.

Целевой префикс один — GH_POOL_. До слияния имён было три семьи: GH_CHROME_*
у браузерной части, POOL_* у клиента и воркера, и голые имена вроде DATA_DIR
у сервера задач. Все три лежат в проде прямо сейчас.

Поэтому старые имена читаются как запасной вариант и один раз пишут
предупреждение. Иначе переключение превратилось бы в одновременную замену
кода и всех переменных сразу — а это ровно тот случай, когда при откате
непонятно, что именно сломалось.
"""

import logging
import os
from collections.abc import Mapping, MutableMapping

log = logging.getLogger(__name__)

PREFIX = "GH_POOL_"

# Имена, где простой обмен префикса дал бы столкновение двух разных секретов
# или двух разных смыслов. Отображение задано поимённо и намеренно.
EXPLICIT: dict[str, str] = {
    # Токен API браузерных сессий и токен воркера пула — разные вещи,
    # а при слепой замене префикса оба стали бы GH_POOL_TOKEN.
    "GH_CHROME_TOKEN": "GH_POOL_TOKEN",
    "POOL_TOKEN": "GH_POOL_WORKER_TOKEN",
    "WORKER_TOKEN": "GH_POOL_WORKER_TOKEN",
    "POOL_CLIENT_TOKEN": "GH_POOL_CLIENT_TOKEN",
    "CLIENT_TOKEN": "GH_POOL_CLIENT_TOKEN",
    # Адрес сервера: клиент и воркер звали его POOL_SERVER, браузерный
    # клиент — GH_CHROME_URL.
    "POOL_SERVER": "GH_POOL_SERVER",
    "GH_CHROME_URL": "GH_POOL_SERVER",
    # Одна база на оба домена, значит и переменная одна.
    "GH_CHROME_DATABASE_URL": "GH_POOL_DATABASE_URL",
    "DATABASE_URL": "GH_POOL_DATABASE_URL",
    # Голые имена сервера задач: слишком общие, чтобы жить без префикса.
    "DATA_DIR": "GH_POOL_DATA_DIR",
    "BLOB_DIR": "GH_POOL_BLOB_DIR",
    "EVENT_CAP": "GH_POOL_EVENT_CAP",
    "LOST_AFTER": "GH_POOL_LOST_AFTER",
    "LEASE_WAIT": "GH_POOL_LEASE_WAIT",
    "WORKER_STALE": "GH_POOL_WORKER_STALE",
    "FLUSH_EVERY": "GH_POOL_FLUSH_EVERY",
    "WORKER_ID": "GH_POOL_WORKER_ID",
    "WORKER_MAX_AGE": "GH_POOL_WORKER_MAX_AGE",
    "SPOOL_DIR": "GH_POOL_SPOOL_DIR",
    "SPOOL_CAP": "GH_POOL_SPOOL_CAP",
    "POOL_TASKS": "GH_POOL_TASKS",
    "POOL_TASK": "GH_POOL_TASK",
    "POOL_DEPS": "GH_POOL_DEPS",
}

# Для всего остального хватает обмена префикса.
RENAMED_PREFIXES: tuple[str, ...] = ("GH_CHROME_",)


def _target(name: str) -> str | None:
    if name in EXPLICIT:
        return EXPLICIT[name]
    for old in RENAMED_PREFIXES:
        if name.startswith(old):
            return PREFIX + name[len(old) :]
    return None


def adopt(env: MutableMapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Заполнить GH_POOL_* из старых имён там, где новое ещё не задано.

    Возвращает список выполненных подстановок. Новое имя всегда сильнее:
    если задано и оно, и старое, старое молча игнорируется — иначе откат
    на старую переменную нельзя было бы сделать, не убрав новую.
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    moved: list[tuple[str, str]] = []
    for name in sorted(target):
        new = _target(name)
        if new is None or new == name or target.get(new):
            continue
        target[new] = target[name]
        moved.append((name, new))
    if moved:
        log.warning(
            "переменные окружения читаются под старыми именами: %s; "
            "переименуйте их в GH_POOL_*",
            ", ".join(f"{old} -> {new}" for old, new in moved),
        )
    return moved


def describe(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    source: Mapping[str, str] = os.environ if env is None else env
    return [
        (name, new)
        for name in sorted(source)
        if (new := _target(name)) is not None and new != name and not source.get(new)
    ]
