from typing import Any

from sqlalchemy import BigInteger, Float, Select, String, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from gh_pool.db.base import Base
from gh_pool.db.engine import session


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), index=True)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[float] = mapped_column(Float, index=True)
    started_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)


class Artifact(Base):
    __tablename__ = "artifacts"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)


TASK_COLUMNS = tuple(Task.__table__.columns.keys())
ARTIFACT_COLUMNS = tuple(Artifact.__table__.columns.keys())


def key_of(model: type[Base]) -> str:
    return next(iter(model.__table__.primary_key.columns)).name  # pyright: ignore[reportAttributeAccessIssue]


def as_dict(row: Base, model: type[Base]) -> dict[str, Any]:
    return {c: getattr(row, c) for c in model.__table__.columns.keys()}  # noqa: SIM118


async def save(model: type[Base], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    key = key_of(model)
    stmt = insert(model).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[key], set_={c: stmt.excluded[c] for c in rows[0] if c != key}
    )
    async with session().begin() as db:
        await db.execute(stmt)


async def fetch(model: type[Base], value: Any) -> dict[str, Any] | None:
    async with session()() as db:
        row = await db.get(model, value)
        return None if row is None else as_dict(row, model)


async def drop(model: type[Base], value: Any) -> None:
    async with session().begin() as db:
        await db.execute(delete(model).where(getattr(model, key_of(model)) == value))


async def rows(model: type[Base], query: Select[Any]) -> list[dict[str, Any]]:
    async with session()() as db:
        return [as_dict(r, model) for r in await db.scalars(query)]


async def tasks(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    q = select(Task).order_by(Task.created_at.desc()).limit(limit)
    return await rows(Task, q.where(Task.status == status) if status else q)


async def artifacts(prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
    q = select(Artifact).order_by(Artifact.created_at.desc()).limit(limit)
    return await rows(
        Artifact, q.where(Artifact.key.startswith(prefix)) if prefix else q
    )


async def unfinished() -> list[dict[str, Any]]:
    return await rows(Task, select(Task).where(Task.status.in_(("pending", "running"))))
