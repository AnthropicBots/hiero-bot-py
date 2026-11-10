"""add stripe_customer_id to accounts

Revision ID: a1c3f8e92b74
Revises: c2b6f4d81a3e
Create Date: 2026-10-17 13:35:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c3f8e92b74"
down_revision = "c2b6f4d81a3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "stripe_customer_id",
            sa.String(255),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_accounts_stripe_customer_id",
        "accounts",
        ["stripe_customer_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_stripe_customer_id", table_name="accounts")
    op.drop_column("accounts", "stripe_customer_id")