"""add stripe webhook event records

Revision ID: 7c1e8f4a2b3d
Revises: 38b5a2a98694
Create Date: 2026-09-10 15:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7c1e8f4a2b3d"
down_revision: str | None = "38b5a2a98694"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stripe_events",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_stripe_events_event_type",
        "stripe_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_stripe_events_processed_at",
        "stripe_events",
        ["processed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stripe_events_processed_at",
        table_name="stripe_events",
    )
    op.drop_index(
        "ix_stripe_events_event_type",
        table_name="stripe_events",
    )
    op.drop_table("stripe_events")