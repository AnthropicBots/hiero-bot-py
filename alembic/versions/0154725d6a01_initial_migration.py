"""initial migration

Revision ID: 0154725d6a01
Revises: 
Create Date: 2026-08-07 15:35:03.623472

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0154725d6a01'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'accounts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('github_installation_id', sa.BigInteger(), nullable=False),
        sa.Column('org_login', sa.String(length=128), nullable=False),
        sa.Column('plan_tier', sa.String(length=32), nullable=False, server_default='free'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('github_installation_id'),
    )
    op.create_index(op.f('ix_accounts_github_installation_id'), 'accounts', ['github_installation_id'], unique=True)
    op.create_index(op.f('ix_accounts_org_login'), 'accounts', ['org_login'], unique=False)

    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('github_user_id', sa.BigInteger(), nullable=False),
        sa.Column('github_login', sa.String(length=128), nullable=False),
        sa.Column('github_email', sa.String(length=256), nullable=True),
        sa.Column('encrypted_oauth_token', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('github_user_id'),
    )
    op.create_index(op.f('ix_users_github_user_id'), 'users', ['github_user_id'], unique=True)
    op.create_index(op.f('ix_users_github_login'), 'users', ['github_login'], unique=False)

    op.create_table(
        'account_users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('authorized', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'user_id', name='uq_account_user'),
    )
    op.create_index(op.f('ix_account_users_account_id'), 'account_users', ['account_id'], unique=False)
    op.create_index(op.f('ix_account_users_user_id'), 'account_users', ['user_id'], unique=False)

    op.create_table(
        'account_repos',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('repo_name', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'repo_name', name='uq_account_repo'),
    )
    op.create_index(op.f('ix_account_repos_account_id'), 'account_repos', ['account_id'], unique=False)
    op.create_index(op.f('ix_account_repos_repo_name'), 'account_repos', ['repo_name'], unique=False)

    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=128), nullable=False),
        sa.Column('actor', sa.String(length=64), nullable=False, server_default='hiero-bot'),
        sa.Column('target_number', sa.Integer(), nullable=True),
        sa.Column('target_login', sa.String(length=128), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
    op.create_index(op.f('ix_audit_logs_owner'), 'audit_logs', ['owner'], unique=False)
    op.create_index(op.f('ix_audit_logs_repo'), 'audit_logs', ['repo'], unique=False)
    op.create_index(op.f('ix_audit_logs_target_login'), 'audit_logs', ['target_login'], unique=False)
    op.create_index(op.f('ix_audit_logs_timestamp'), 'audit_logs', ['timestamp'], unique=False)
    op.create_index('ix_audit_owner_repo', 'audit_logs', ['owner', 'repo'], unique=False)

    op.create_table(
        'pr_health_scores',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=128), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('pr_author', sa.String(length=128), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('has_tests', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('has_linked_issue', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('has_description', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('dco_signed', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('review_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('files_changed', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('label_applied', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_pr_health_scores_owner'), 'pr_health_scores', ['owner'], unique=False)
    op.create_index(op.f('ix_pr_health_scores_repo'), 'pr_health_scores', ['repo'], unique=False)
    op.create_index('ix_pr_health_owner_repo', 'pr_health_scores', ['owner', 'repo'], unique=False)

    op.create_table(
        'contributor_snapshots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=128), nullable=False),
        sa.Column('login', sa.String(length=128), nullable=False),
        sa.Column('merged_prs', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('reviews_given', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('months_active', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('current_role', sa.String(length=32), nullable=False, server_default='contributor'),
        sa.Column('eligible_for', sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_contributor_snapshots_login'), 'contributor_snapshots', ['login'], unique=False)
    op.create_index(op.f('ix_contributor_snapshots_owner'), 'contributor_snapshots', ['owner'], unique=False)
    op.create_index(op.f('ix_contributor_snapshots_repo'), 'contributor_snapshots', ['repo'], unique=False)

    op.create_table(
        'stale_action_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=128), nullable=False),
        sa.Column('issue_number', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('days_inactive', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stale_owner_repo', 'stale_action_logs', ['owner', 'repo'], unique=False)

    op.create_table(
        'reviewer_recommendations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=128), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('recommended_reviewer', sa.String(length=128), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('was_assigned', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('reviewer_recommendations')
    op.drop_table('stale_action_logs')
    op.drop_table('contributor_snapshots')
    op.drop_table('pr_health_scores')
    op.drop_table('audit_logs')
    op.drop_table('account_repos')
    op.drop_table('account_users')
    op.drop_table('users')
    op.drop_table('accounts')
