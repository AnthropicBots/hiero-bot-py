"""add pending issue close notices

Revision ID: b7e2c4a91d60
Revises: a1c3f8e92b74
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e2c4a91d60"
down_revision: str | None = "a1c3f8e92b74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_issue_close_notices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("repo", sa.String(length=128), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("days_inactive", sa.Integer(), nullable=False),
        sa.Column("last_activity", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner",
            "repo",
            "issue_number",
            name="uq_pending_close_notice_owner_repo_issue",
        ),
    )
    op.create_index(
        "ix_pending_close_notice_owner_repo",
        "pending_issue_close_notices",
        ["owner", "repo"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_close_notice_owner_repo",
        table_name="pending_issue_close_notices",
    )
    op.drop_table("pending_issue_close_notices")
