"""add lifecycle actions

Revision ID: c9d4e1f07a52
Revises: b7e2c4a91d60
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d4e1f07a52"
down_revision: str | None = "b7e2c4a91d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("repo", sa.String(length=128), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("last_activity", sa.DateTime(), nullable=True),
        sa.Column("notice_delivered_at", sa.DateTime(), nullable=True),
        sa.Column("notice_attempts", sa.Integer(), nullable=False),
        sa.Column("settled_updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_lifecycle_action_issue",
        "lifecycle_actions",
        ["owner", "repo", "issue_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_lifecycle_action_issue", table_name="lifecycle_actions")
    op.drop_table("lifecycle_actions")
