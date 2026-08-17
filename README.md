# pool

GitHub Actions как пул универсальных воркеров. Центральный сервер держит очередь,
раннеры поллят её и выполняют задачи по типам.

```
клиент ──POST /v1/tasks──> сервер <──lease/heartbeat── 20 раннеров в matrix
   │                          │                              │
   └──tail логов по офсету────┘<────логи + результат─────────┘
```

## Принципы

- Ретраев нет. Раннер умер — задача становится `lost`, перезапуск руками через `pool retry`.
- Воркер считает сервер вечно живым: сеть и 5xx ретраятся бесконечно с джиттером.
- Время жизни раннера не учитывается, задачи сами отвечают за свою длительность.
- Логи — тупой аппенд байтов в файл, дедупликация по офсету.

## Статусы

`pending → running → done | failed | cancelled | lost`

`lost` ставит reaper, если хартбита не было `LOST_AFTER` секунд.

## Запуск

```bash
uv sync
cp .env.example .env

uv run pool-server
POOL_TOKEN=... uv run pool-worker
POOL_CLIENT_TOKEN=... uv run pool submit echo -p count=5 -f
```

## CLI

```bash
pool submit <type> -p key=val [-f]     отправить, -f = следить за логом
pool submit <type> --payload '{...}'
pool logs <id> [-f] [-o offset]
pool status <id>
pool list [-s running] [-n 50]
pool result <id> [-o file]
pool cancel <id>
pool retry <id> [-f]
pool workers
pool health
```

Коды возврата при `-f`: 0 done, 1 failed, 2 cancelled, 3 lost.

## Свои задачи

`src/pool/tasks.py`, сигнатура `fn(payload, result_path)`. Всё, что уходит
в stdout и stderr, становится логом задачи. Возвращённое значение пишется
в результат, большие файлы пиши сам в `result_path`.

```python
@task("myjob")
def myjob(payload, result_path):
    print("работаю")
    return {"ok": True}
```

Свой модуль вместо встроенного — через `POOL_TASKS=mypkg.tasks`.

Отмена приходит как SIGTERM, её можно поймать для уборки. Через 30 секунд SIGKILL.

## Деплой раннеров

`.github/workflows/pool.yml` в репу, секреты `POOL_SERVER` и `POOL_TOKEN`.
Волна из 20 джоб раз в 6 часов, `concurrency` гарантирует одну волну за раз.

Помни: cron отключается после 60 дней без коммитов в репу и опаздывает до получаса.
Лимит конкурентных джоб — 20 на free, 40 на Pro.

## Переменные

Сервер: `WORKER_TOKEN`, `CLIENT_TOKEN`, `DATA_DIR`, `DB_PATH`, `LOG_CAP`,
`LOST_AFTER`, `LEASE_WAIT`, `HOST`, `PORT`.

Воркер: `POOL_SERVER`, `POOL_TOKEN`, `WORKER_ID`, `SPOOL_DIR`, `SPOOL_CAP`, `POOL_TASKS`.

Клиент: `POOL_SERVER`, `POOL_CLIENT_TOKEN`.
