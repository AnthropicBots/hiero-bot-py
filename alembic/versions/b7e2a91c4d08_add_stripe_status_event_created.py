"""store the Stripe event time used for entitlement updates

Revision ID: b7e2a91c4d08
Revises: c2b6f4d81a3e
Create Date: 2026-10-15 10:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e2a91c4d08"
down_revision: str | None = "c2b6f4d81a3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column("stripe_status_event_created", sa.BigInteger(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("stripe_status_event_created")
