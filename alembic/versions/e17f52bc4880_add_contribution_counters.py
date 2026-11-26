"""Add per-repository contribution counters.

Revision ID: e17f52bc4880
Revises: c9d4e1f07a52
"""

import sqlalchemy as sa

from alembic import op

revision = "e17f52bc4880"
down_revision = "c9d4e1f07a52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_repos", sa.Column(
        "contribution_backfill_page", sa.Integer(), server_default="1", nullable=False,
    ))
    op.create_table(
        "counted_merges",
        sa.Column("owner", sa.String(128), primary_key=True),
        sa.Column("repo", sa.String(128), primary_key=True),
        sa.Column("pr_number", sa.Integer(), primary_key=True),
        sa.Column("author", sa.String(128), nullable=False),
    )
    op.create_index("ix_counted_merge_author", "counted_merges", ["owner", "repo", "author"])
    op.create_table(
        "counted_reviews",
        sa.Column("owner", sa.String(128), primary_key=True),
        sa.Column("repo", sa.String(128), primary_key=True),
        sa.Column("pr_number", sa.Integer(), primary_key=True),
        sa.Column("reviewer", sa.String(128), primary_key=True),
    )
    op.create_index("ix_counted_review_reviewer", "counted_reviews", ["owner", "repo", "reviewer"])


def downgrade() -> None:
    op.drop_table("counted_reviews")
    op.drop_table("counted_merges")
    op.drop_column("account_repos", "contribution_backfill_page")
