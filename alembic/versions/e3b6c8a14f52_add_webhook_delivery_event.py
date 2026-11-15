"""Add event to webhook deliveries.

Revision ID: e3b6c8a14f52
Revises: a1c3f8e92b74
"""

import sqlalchemy as sa

from alembic import op

revision = "e3b6c8a14f52"
down_revision = "a1c3f8e92b74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing deliveries have no recorded event.
    op.add_column("webhook_deliveries", sa.Column("event", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("webhook_deliveries") as batch_op:
        batch_op.drop_column("event")
