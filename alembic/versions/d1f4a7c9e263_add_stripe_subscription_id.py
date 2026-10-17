"""associate Stripe subscriptions with accounts

Revision ID: d1f4a7c9e263
Revises: b7e2a91c4d08
Create Date: 2026-10-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1f4a7c9e263"
down_revision: str | None = "b7e2a91c4d08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column("stripe_subscription_id", sa.String(255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("stripe_subscription_created", sa.BigInteger(), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_accounts_stripe_subscription_id",
            ["stripe_subscription_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint(
            "uq_accounts_stripe_subscription_id",
            type_="unique",
        )
        batch_op.drop_column("stripe_subscription_created")
        batch_op.drop_column("stripe_subscription_id")
