"""add webhook_deliveries table

Revision ID: 9f1c3a7d5e21
Revises: 7c1e8f4a2b3d
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f1c3a7d5e21"
down_revision: str | None = "7c1e8f4a2b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_deliveries_received_at",
        "webhook_deliveries",
        ["received_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_deliveries_received_at",
        table_name="webhook_deliveries",
    )
    op.drop_table("webhook_deliveries")