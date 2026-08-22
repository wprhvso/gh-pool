import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

NO_DATABASE = (
    "no postgres to test against: install one, or point "
    "GH_POOL_TEST_DATABASE_URL at a cluster"
)


def _run(*command: str) -> None:
    finished = subprocess.run(command, capture_output=True, check=False, text=True)
    if finished.returncode != 0:
        raise RuntimeError(f"{command[0]} failed: {finished.stderr.strip()[:400]}")


def _postgres_bin() -> Path | None:
    found = shutil.which("initdb")
    if found is not None:
        return Path(found).parent
    packaged = sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    return packaged[0].parent if packaged else None


def _with_dbname(url: str, name: str) -> str:
    params: dict[str, Any] = dict(conninfo_to_dict(url))
    params["dbname"] = name
    return make_conninfo(**params)


class Cluster:
    def __init__(self, url: str, stop: Callable[[], None] | None = None) -> None:
        self.url = url
        self._stop = stop

    def create(self, name: str) -> str:
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        return _with_dbname(self.url, name)

    def drop(self, name: str) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("drop database if exists {} with (force)").format(
                    sql.Identifier(name)
                )
            )

    def stop(self) -> None:
        if self._stop is not None:
            self._stop()


def start_cluster(base: Path) -> Cluster | None:
    given = os.environ.get("GH_POOL_TEST_DATABASE_URL")
    if given:
        return Cluster(given)
    binaries = _postgres_bin()
    if binaries is None or os.geteuid() == 0:
        return None
    data = base / "pgdata"
    sockets = Path(tempfile.mkdtemp(prefix="pgs"))
    _run(
        str(binaries / "initdb"),
        "-D", str(data),
        "-A", "trust",
        "-U", "postgres",
        "--no-sync",
    )  # fmt: skip
    options = f"-k {sockets} -h '' -c fsync=off -c full_page_writes=off"
    _run(
        str(binaries / "pg_ctl"),
        "-D", str(data),
        "-l", str(base / "postgres.log"),
        "-o", options,
        "-w", "start",
    )  # fmt: skip

    def stop() -> None:
        with contextlib.suppress(RuntimeError):
            _run(str(binaries / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop")
        shutil.rmtree(sockets, ignore_errors=True)

    return Cluster(f"postgresql://postgres@/postgres?host={sockets}", stop)
