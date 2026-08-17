# pool

GitHub Actions как пул универсальных воркеров. Центральный сервер держит очередь,
раннеры поллят её и выполняют питон-код, приезжающий вместе с задачей.

```
клиент ──POST /v1/tasks──> сервер <──lease/heartbeat── 20 раннеров
   │                          │                              │
   └──чтение по офсету────────┘<────поток событий────────────┘
```

## Принципы

- Задача — это код, а не тип на сервере. Пул ничего не знает о предметной области.
- Ретраев нет. Раннер умер — задача становится `lost`, перезапуск руками через `pool retry`.
- Воркер считает сервер вечно живым: сеть и 5xx ретраятся бесконечно с джиттером.
- Время жизни раннера не учитывается, задачи сами отвечают за свою длительность.
- События — тупой аппенд байтов в файл, дедупликация по офсету.
- Обратный канал один и тот же на всё: событие — строка `::pool::{...}` среди обычного вывода.
- Результата как сущности нет. Есть поток, а что в нём считать ответом — дело клиента.
- База — проекция, а не источник правды. Очередь живёт в памяти, Postgres догоняет её фоном.

## Статусы

`pending → running → done | failed | cancelled | lost`

`lost` ставит reaper, если хартбита не было `LOST_AFTER` секунд.

## Запуск

```bash
uv sync
cp .env.example .env
createdb pool                                 # нужен Postgres

uv run pool-server
POOL_TOKEN=... uv run pool-worker
POOL_CLIENT_TOKEN=... uv run pool submit python -p code='result = 2 + 2' -f
```

## SDK

```python
from pool.sdk import Pool

pool = Pool("https://pool.example.com", token="...")


@pool.remote(deps=["httpx"], timeout=300)
def title(url):
    import re

    import httpx

    html = httpx.get(url, timeout=30).text
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1)


task = title.submit("https://example.com")    # отдать и не ждать
for event in task.watch():                    # события по мере поступления
    print(event)

done = title("https://example.com")           # выполнить и дождаться
done.events()                                 # [{'kind': 'result', 'value': ..., 'at': ...}]
tasks = title.map(urls)                       # по задаче на элемент, разом на весь пул
```

Функция уезжает на раннер исходником, поэтому обязана быть самодостаточной:
импорты внутри, никаких замыканий и глобалок из твоего модуля. Аргументы и
возвращаемое значение ездят как JSON.

`Remote` возвращает задачи, а не значения: `submit` отдаёт хэндл сразу, `__call__`
и `map` ждут завершения и отдают его же. Что вынуть из потока — решаешь сам:

```python
def value(task):
    return next((e["value"] for e in reversed(task.events()) if e["kind"] == "result"), None)
```

- `pool.run(fn, *args, **kwargs)`, `pool.submit(...)`, `pool.map(fn, items)` — то же самое без декоратора.
- Вместо функции можно дать строку кода: `pool.run("result = sum(args)", 1, 2, 3)`.
- В `map` кортеж раскладывается в позиционные аргументы, `spawn` делает то же самое, но не ждёт.
- `task.watch()` отдаёт события по мере поступления, `task.events()` — всё, что уже накопилось.
- `task.follow()` отдаёт сырые байты кусками, `task.raw()` — весь поток, `task.wait()` ждёт терминального статуса, `task.cancel()` отменяет.
- Упавшая задача поднимает `Failed`: в `.event` структурная причина, в `.tail` хвост потока с трейсбеком. Поднимает его `task.check()`, его же зовут `__call__` и `map`.
- Пакет стоит и на раннере, так что задача может сама разложить подзадачи по пулу.
- `pool.put`, `pool.get`, `pool.download`, `pool.delete`, `pool.artifacts` — файловое хранилище, см. ниже.

## Задачи

Тип задачи ровно один — `python`, всё остальное живёт в payload:

| поле      | что                                          | по умолчанию            |
| --------- | -------------------------------------------- | ----------------------- |
| `code`    | исходник                                     | обязателен              |
| `entry`   | имя функции, которую звать                   | нет, берётся `result`   |
| `args`    | позиционные аргументы                        | `[]`                    |
| `kwargs`  | именованные аргументы                        | `{}`                    |
| `deps`    | зависимости, ставятся перед запуском         | `[]`                    |
| `timeout` | секунд на выполнение                         | без ограничения         |

Без `entry` код исполняется целиком, результатом становится переменная `result`,
а `args` и `kwargs` доступны как глобальные.

`deps` ставятся в `POOL_DEPS` (по умолчанию `/tmp/pool-deps`) и кэшируются по
набору, так что одинаковые зависимости ставятся один раз на жизнь раннера.

Отмена приходит как SIGTERM, её можно поймать для уборки. Через 30 секунд SIGKILL.
`timeout` поднимает `TimeoutError` внутри задачи, не дожидаясь смерти раннера.

Нужен свой набор типов вместо встроенного — `POOL_TASKS=mypkg.tasks`, там словарь
`REGISTRY` из имени в `fn(payload)`.

## Обратный канал

Задача отдаёт JSON когда хочет, а не только в конце:

```python
from pool import emit

emit("status", "качаю")
emit("progress", done=3, total=10)
emit("result", {"rows": 10})
```

`emit(kind, value=None, **fields)` печатает одну строку `::pool::{...}` в stdout,
поэтому события едут тем же потоком, что и обычный `print`, в том же порядке и с
той же дедупликацией по офсету. Годится откуда угодно: из потоков — вокруг записи
есть блокировка, из дочерних процессов и чужих библиотек — лишь бы stdout был общий.

Отдельного канала результатов нет: `result` — просто соглашение об имени события,
и клиент сам решает, какое из них ответ. Возвращённое из функции значение уезжает
таким же событием само, так что `return x` и `emit("result", x)` — одно и то же,
а промежуточные результаты можно слать по ходу дела. Возвращаемое обязано быть
JSON-сериализуемым, большое и бинарное кладётся в артефакты; поля событий, если
в JSON не лезут, приводятся к строке, чтобы телеметрия не роняла задачу.

Событие `error` с типом и сообщением пул добавляет сам, когда задача падает, — на
клиенте причина видна программно, без разбора трейсбека глазами.

Всё, что уходит в stdout и stderr помимо событий, остаётся в потоке обычным текстом.

## Артефакты

Тупой key-value поверх диска: положил файл по ключу, забрал по тому же ключу.

```python
pool.put("in/data.csv", Path("data.csv"))    # файл, байты или строка
pool.get("in/data.csv")                      # -> bytes
pool.download("in/data.csv", "copy.csv")     # потоком в файл
pool.artifacts(prefix="in/")                 # размер, sha256, время, задача
pool.delete("in/data.csv")
```

Из задачи то же самое и без единой настройки — адрес и токен берутся из окружения
раннера, а ключ сам привязывается к задаче, которая его положила:

```python
@pool.remote
def crunch(key):
    from pool.rpc import download, put

    download(key, "/tmp/in.csv")
    ...
    put("out/report.parquet", Path("/tmp/report.parquet"))
```

Ключ — любая строка, слэши и юникод разрешены, перезапись побеждает последняя.
Байты лежат в `BLOB_DIR`, путь считается из sha256 ключа, все обращения к файлам
уходят в отдельные потоки и лупа не трогают. В базе на каждый ключ строка: размер,
sha256, время и задача-создатель. Загрузка из задачи отмечается событием `artifact`
в её потоке.

Так и возвращают большое: `emit("result", ...)` для JSON, артефакт для всего
остального.

## Хранилище

Метаданные лежат в Postgres через SQLAlchemy: `tasks` со всеми задачами — идущими,
готовыми, упавшими и потерянными, — и `artifacts` с ключами.

База при этом не стоит на пути задач. Очередь, лизы, хартбиты и отмены живут в
памяти сервера, а сливает их в Postgres фоновый писатель раз в `FLUSH_EVERY` одной
пачкой. Ни один запрос воркера базу не ждёт: сабмит, лиз, хартбит и завершение —
это правка словаря в памяти.

Отсюда главное свойство: если база легла, пул продолжает молотить задачи, а записи
копятся и доливаются, когда база вернётся. `pool health` показывает и `db`, и
`pending_writes`, так что состояние видно.

Цена — падение самого сервера теряет последние сотни миллисекунд изменений. При
старте сервер поднимает из базы незавершённое: `pending` возвращаются в очередь,
а бывшие `running` помечаются `lost` — их лиз-токен утрачен, и воркеры их бросят.

## CLI

```bash
pool submit python -p code='result = 2 + 2' [-f]   отправить, -f = следить за потоком
pool submit python --payload-file task.json
pool events <id> [-f] [-o offset]
pool status <id>
pool list [-s running] [-n 50]
pool cancel <id>
pool retry <id> [-f]
pool put <key> <file> [-t task]
pool get <key> [-o file]
pool rm <key>
pool artifacts [prefix] [-n 50]
pool workers
pool health
```

Коды возврата при `-f`: 0 done, 1 failed, 2 cancelled, 3 lost.

## Деплой раннеров

Раннеры разворачивает и стережёт `pool-keeper`: в конфиге репы и токены к ним,
дальше он сам создаёт публичные репы, кладёт воркфлоу, ставит секреты и держит
ровно 20 живых джоб на аккаунт, поднимая упавшие.

```bash
cp keeper.toml.example keeper.toml

uv run pool-keeper build -c keeper.toml
uv run pool-keeper run -c keeper.toml
```

Помни: лимит конкурентных джоб — 20 на free, 40 на Pro, потолок одной джобы — 6 часов.

## Nix

`flake.nix` собирает пакет из `uv.lock` через uv2nix, так что версии в Nix ровно те
же, что у `uv sync`, и второй список зависимостей вести не нужно.

```bash
nix build            # venv со всеми точками входа в result/bin
nix develop          # то же плюс uv, ruff и postgres под тесты
nix run .# -- health
```

Два модуля NixOS: `nixosModules.server` и `nixosModules.client`.

```nix
{
  inputs.pool.url = "github:wprhvso/pool";

  # сервер: демон, база и юзер заводятся сами
  imports = [ pool.nixosModules.server ];
  services.pool.server = {
    enable = true;
    host = "0.0.0.0";
    openFirewall = true;
    environmentFile = "/run/secrets/pool";   # WORKER_TOKEN и CLIENT_TOKEN
    settings.FLUSH_EVERY = "0.1";
  };
}
```

`postgresql = true` по умолчанию поднимает локальный Postgres и заводит в нём базу
с ролью, доступ идёт юникс-сокетом. Своя база — снимите флаг и задайте
`databaseUrl`. Токены в стор не кладутся, их читает systemd из `environmentFile`.

Клиентская сторона — CLI и два демона, каждый включается отдельно:

```nix
{
  imports = [ pool.nixosModules.client ];

  programs.pool = {
    enable = true;                            # обёртка pool с адресом и токеном
    server = "https://pool.example.com";
    tokenFile = "/run/secrets/pool-client";
  };

  services.pool.worker = {
    enable = true;                            # локальный раннер рядом с гитхабовскими
    server = "https://pool.example.com";
    environmentFile = "/run/secrets/pool-worker";
  };

  services.pool.keeper = {
    enable = true;                            # держит фронт раннеров на гитхабе
    configFile = "/run/secrets/keeper.toml";
    build = true;                             # ещё и разложить воркфлоу с секретами
  };
}
```

Воркер выполняет присланный код, поэтому крутится под `DynamicUser` со своим
кэшем и приватным `/tmp`. Изоляция это всё равно слабая: не ставьте воркер на
машину, где есть что терять.

Кипер тоже под `DynamicUser`, а конфиг заезжает к нему кредом systemd: файл
читает root до сброса прав, так что `configFile` спокойно лежит под `0400
root:root` рядом с остальными секретами. `build = true` перед каждым запуском
делает то же, что `pool-keeper build` руками, — заводит недостающие репы,
обновляет воркфлоу и ставит в них `POOL_SERVER` с `POOL_TOKEN`, — так что фронт
раннеров поднимается с нуля одним `nixos-rebuild`. Трогает он только те репы,
что перечислены в конфиге. Воркфлоу берётся из `workflows`, по умолчанию — из
самого флейка.

## Переменные

Сервер: `DATABASE_URL`, `WORKER_TOKEN`, `CLIENT_TOKEN`, `DATA_DIR`, `BLOB_DIR`,
`EVENT_CAP`, `FLUSH_EVERY`, `LOST_AFTER`, `LEASE_WAIT`, `WORKER_STALE`, `HOST`, `PORT`.

Воркер: `POOL_SERVER`, `POOL_TOKEN`, `WORKER_ID`, `SPOOL_DIR`, `SPOOL_CAP`,
`POOL_TASKS`, `POOL_DEPS`.

Клиент: `POOL_SERVER`, `POOL_CLIENT_TOKEN`.
