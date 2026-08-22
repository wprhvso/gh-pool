from __future__ import annotations

import argparse
import importlib.metadata
import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from yaol import from_env, instrument_runtime, setup, shutdown

from gh_pool.fleet.runners.budget import REST
from gh_pool.fleet.runners.config import (
    RATE_WAIT_CAP,
    RESTART_CAP,
    RESTART_HEALTHY,
    Server,
    debug,
    env_server,
    env_target,
    load,
)
from gh_pool.fleet.runners.controller import install_stop_handler, run
from gh_pool.fleet.runners.errors import RunnerError
from gh_pool.fleet.runners.gh import preflight, release_version
from gh_pool.fleet.runners.http import backoff
from gh_pool.fleet.runners.pool import Pool

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gh_pool.fleet.runners.config import Target

log = logging.getLogger("runners")

_EXIT_USAGE = 2
_EXIT_ERROR = 1


def _version() -> str:
    try:
        return importlib.metadata.version("gh-pool")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _observe() -> None:
    config = from_env("pool-runners", service_version=_version())
    if debug():
        config = replace(config, log_level="DEBUG")
    setup(config)
    instrument_runtime()


def _attempt(
    target: Target, server: Server, stop: threading.Event, results: dict[str, int]
) -> bool:
    try:
        results[target.slug] = run(target, server, stop)
    except RunnerError as exc:
        log.error("%s: %s", target.slug, exc)
    except Exception:
        log.exception("%s: непредвиденный сбой", target.slug)
    else:
        return True
    results[target.slug] = _EXIT_ERROR
    return False


def _worker(
    target: Target, server: Server, stop: threading.Event, results: dict[str, int]
) -> None:
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        if _attempt(target, server, stop, results):
            return
        if time.monotonic() - started >= RESTART_HEALTHY:
            failures = 0
        failures += 1

        waiting = REST.shut()
        if waiting:
            delay = min(waiting, RATE_WAIT_CAP)
            log.warning(
                "лимит REST закрыт, старт через %.0f с — %s", delay, REST.state()
            )
        else:
            delay = backoff(failures, cap=RESTART_CAP)
            log.warning(
                "%s: перезапуск через %.1f с (сбой %s подряд)",
                target.slug,
                delay,
                failures,
            )
        if stop.wait(delay):
            break

    if not results.get(target.slug):
        return
    log.info("%s: остановили на перезапуске, всё-таки уберу за собой", target.slug)
    _attempt(target, server, stop, results)


def _check(targets: list[Target], server: Server) -> int:
    ok = True
    pool = Pool(server)
    try:
        health = pool.health()
        log.info(
            "пул %s: задач %s, воркеров %s",
            pool.url,
            health.get("tasks"),
            health.get("workers"),
        )
        if not health.get("workers"):
            log.warning("в пуле нет живых воркеров — раннерам некуда ехать")
    except RunnerError as exc:
        log.error("пул %s недоступен: %s", pool.url, exc)
        ok = False

    for target in targets:
        try:
            info = preflight(target)
            version = target.version or release_version()
            log.info(
                "%s: доступ есть, %s, метка %r, раннеров до %s, версия %s",
                target.slug,
                "приватная" if info.get("private") else "ПУБЛИЧНАЯ",
                target.label,
                target.jobs,
                version,
            )
        except RunnerError as exc:
            log.error("%s: %s", target.slug, exc)
            ok = False
    return 0 if ok else _EXIT_ERROR


def _targets(args: argparse.Namespace) -> tuple[list[Target], Server]:
    if args.config and args.repos:
        raise RunnerError("или конфиг, или репозитории в аргументах")
    if args.config:
        if args.config.exists() and args.config.stat().st_mode & 0o077:
            log.warning(
                "в %s лежат токены, а читать его может кто угодно — chmod 600",
                args.config,
            )
        return load(args.config)
    if not args.repos:
        raise RunnerError("нечего сторожить: дай owner/name или -c конфиг")
    return [env_target(slug) for slug in args.repos], env_server()


def _serve(args: argparse.Namespace) -> int:
    try:
        targets, server = _targets(args)
    except RunnerError as exc:
        sys.stderr.write(f"{exc}\n")
        return _EXIT_USAGE

    if args.check:
        return _check(targets, server)

    stop = install_stop_handler()
    results: dict[str, int] = {}
    threads = [
        threading.Thread(
            target=_worker, args=(target, server, stop, results), name=target.slug
        )
        for target in targets
    ]

    log.info("целей %s, пул %s", len(targets), server.url)
    log.info(
        "остановка: Ctrl+C — доработают job'ы и снесётся scale set, второй Ctrl+C не ждёт"
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return _EXIT_ERROR if any(code != 0 for code in results.values()) else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="pool-runners")
    parser.add_argument("repos", nargs="*")
    parser.add_argument("-c", "--config", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    _observe()
    try:
        return _serve(args)
    finally:
        shutdown()


def cli() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(cli())
