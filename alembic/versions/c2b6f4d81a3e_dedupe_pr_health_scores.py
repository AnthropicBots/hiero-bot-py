"""dedupe pr_health_scores and add unique (owner, repo, pr_number)

Revision ID: c2b6f4d81a3e
Revises: 9f1c3a7d5e21
Create Date: 2026-09-15 00:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2b6f4d81a3e"
down_revision: str | None = "9f1c3a7d5e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM pr_health_scores
        WHERE id NOT IN (
            SELECT MAX(id) FROM pr_health_scores
            GROUP BY owner, repo, pr_number
        )
        """
    )

    with op.batch_alter_table("pr_health_scores") as batch_op:
        batch_op.create_unique_constraint(
            "uq_pr_health_owner_repo_pr_number",
            ["owner", "repo", "pr_number"],
        )


def downgrade() -> None:
    with op.batch_alter_table("pr_health_scores") as batch_op:
        batch_op.drop_constraint(
            "uq_pr_health_owner_repo_pr_number",
            type_="unique",
        )