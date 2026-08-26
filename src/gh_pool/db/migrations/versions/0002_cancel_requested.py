"""cancel_requested survives a restart

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "cancel_requested"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return COLUMN in {c["name"] for c in inspector.get_columns("tasks")}


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(
        "tasks",
        sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    if _has_column():
        op.drop_column("tasks", COLUMN)
