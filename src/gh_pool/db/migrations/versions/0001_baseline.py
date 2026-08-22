"""baseline: обе старые схемы одной ревизией

Revision ID: 0001
Revises:
Create Date: 2026-08-22

Ревизия идемпотентна по построению: каждый объект создаётся только если его
ещё нет. Это нужно потому, что до alembic схема жила в двух местах — pool
поднимал свои таблицы через Base.metadata.create_all, а браузерная часть
своим SQL-раннером с таблицей schema_migrations. На проде обе схемы уже
накатаны, и upgrade head обязан пройти по такой базе не тронув ни таблицы,
ни данных.

Поэтому здесь не autogenerate, а ручной baseline: он одинаково корректен и
на чистой базе, и на базе, стоящей на обеих старых головах.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_type(name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text("select 1 from pg_type where typname = :n"), {"n": name}
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_type("session_status"):
        bind.execute(
            sa.text(
                "create type session_status as enum "
                "('pending', 'active', 'closed', 'dead')"
            )
        )
    if not _has_type("command_status"):
        bind.execute(
            sa.text(
                "create type command_status as enum "
                "('queued', 'started', 'finished', 'failed')"
            )
        )

    session_status = postgresql.ENUM(name="session_status", create_type=False)
    command_status = postgresql.ENUM(name="command_status", create_type=False)
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    ts = sa.DateTime(timezone=True)

    if not _has_table("tasks"):
        op.create_table(
            "tasks",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("type", sa.String(64), nullable=False),
            sa.Column("payload", jsonb, nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("worker_id", sa.String(128)),
            sa.Column("error", sa.Text()),
            sa.Column("parent_id", sa.String(32)),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("started_at", sa.Float()),
            sa.Column("finished_at", sa.Float()),
        )
        op.create_index("ix_tasks_status", "tasks", ["status"])
        op.create_index("ix_tasks_created_at", "tasks", ["created_at"])

    if not _has_table("artifacts"):
        op.create_table(
            "artifacts",
            sa.Column("key", sa.Text(), primary_key=True),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("size", sa.BigInteger(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("task_id", sa.String(32)),
            sa.Column("created_at", sa.Float(), nullable=False),
        )
        op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
        op.create_index("ix_artifacts_created_at", "artifacts", ["created_at"])

    if not _has_table("profiles"):
        op.create_table(
            "profiles",
            sa.Column("name", sa.Text(), primary_key=True),
            sa.Column("size", sa.BigInteger()),
            sa.Column(
                "stale", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column("updated_at", ts),
        )

    if not _has_table("sessions"):
        op.create_table(
            "sessions",
            sa.Column("id", uuid, primary_key=True),
            sa.Column(
                "status",
                session_status,
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column("params", jsonb, nullable=False),
            sa.Column(
                "profile",
                sa.Text(),
                sa.ForeignKey("profiles.name", ondelete="SET NULL"),
            ),
            sa.Column(
                "persist", sa.Boolean(), nullable=False, server_default=sa.text("true")
            ),
            sa.Column(
                "state_stale",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "last_cmd_seq",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("heartbeat_at", ts),
            sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
            sa.Column("ready_at", ts),
            sa.Column("closed_at", ts),
            sa.Column("runner_token", sa.Text()),
        )
        op.create_index(
            "sessions_active_idx",
            "sessions",
            ["status"],
            postgresql_where=sa.text("status in ('pending', 'active')"),
        )

    if not _has_table("commands"):
        op.create_table(
            "commands",
            sa.Column("id", uuid, primary_key=True),
            sa.Column(
                "session_id",
                uuid,
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("seq", sa.BigInteger(), nullable=False),
            sa.Column("method", sa.Text(), nullable=False),
            sa.Column("args", jsonb, nullable=False),
            sa.Column("timeout_ms", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                command_status,
                nullable=False,
                server_default=sa.text("'queued'"),
            ),
            sa.Column("result", jsonb),
            sa.Column("error", jsonb),
            sa.Column("queued_at", ts, nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", ts),
            sa.Column("finished_at", ts),
            sa.Column("traceparent", sa.Text()),
            sa.Column("tracestate", sa.Text()),
            sa.UniqueConstraint("session_id", "seq"),
        )
        op.create_index(
            "commands_queue_idx",
            "commands",
            ["session_id", "seq"],
            postgresql_where=sa.text("status = 'queued'"),
        )
        op.create_index(
            "commands_started_idx",
            "commands",
            ["started_at"],
            postgresql_where=sa.text("status = 'started'"),
        )

    if not _has_table("events"):
        op.create_table(
            "events",
            sa.Column(
                "session_id",
                uuid,
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("seq", sa.BigInteger(), primary_key=True),
            sa.Column("type", sa.Text(), nullable=False),
            sa.Column("data", jsonb, nullable=False),
            sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
        )

    if not _has_table("files"):
        op.create_table(
            "files",
            sa.Column("id", uuid, primary_key=True),
            sa.Column(
                "session_id",
                uuid,
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("size", sa.BigInteger(), nullable=False),
            sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
        )

    if not _has_table("downloads"):
        op.create_table(
            "downloads",
            sa.Column(
                "session_id",
                uuid,
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("name", sa.Text(), primary_key=True),
            sa.Column("size", sa.BigInteger(), nullable=False),
            sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    for table in (
        "downloads",
        "files",
        "events",
        "commands",
        "sessions",
        "profiles",
        "artifacts",
        "tasks",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    bind.execute(sa.text("drop type if exists command_status"))
    bind.execute(sa.text("drop type if exists session_status"))
