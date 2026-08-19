# app/db/models.py — ORM models

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(64), default="hiero-bot")
    target_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_login: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_audit_owner_repo", "owner", "repo"),
    )


class PRHealthScore(Base):
    __tablename__ = "pr_health_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )
    owner: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(128), index=True)
    pr_number: Mapped[int] = mapped_column(Integer)
    pr_author: Mapped[str] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float)
    has_tests: Mapped[bool] = mapped_column(Boolean, default=False)
    has_linked_issue: Mapped[bool] = mapped_column(Boolean, default=False)
    has_description: Mapped[bool] = mapped_column(Boolean, default=False)
    dco_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    label_applied: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_pr_health_owner_repo", "owner", "repo"),
    )


class ContributorSnapshot(Base):
    __tablename__ = "contributor_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    owner: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(128), index=True)
    login: Mapped[str] = mapped_column(String(128), index=True)
    merged_prs: Mapped[int] = mapped_column(Integer, default=0)
    reviews_given: Mapped[int] = mapped_column(Integer, default=0)
    months_active: Mapped[int] = mapped_column(Integer, default=0)
    current_role: Mapped[str] = mapped_column(String(32), default="contributor")
    eligible_for: Mapped[str | None] = mapped_column(String(32), nullable=True)


class StaleActionLog(Base):
    __tablename__ = "stale_action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    owner: Mapped[str] = mapped_column(String(128))
    repo: Mapped[str] = mapped_column(String(128))
    issue_number: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))   # marked_stale | closed | unassigned
    days_inactive: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_stale_owner_repo", "owner", "repo"),
    )


class ReviewerRecommendation(Base):
    __tablename__ = "reviewer_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    owner: Mapped[str] = mapped_column(String(128))
    repo: Mapped[str] = mapped_column(String(128))
    pr_number: Mapped[int] = mapped_column(Integer)
    recommended_reviewer: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    was_assigned: Mapped[bool] = mapped_column(Boolean, default=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    org_login: Mapped[str] = mapped_column(String(128), index=True)
    account_type: Mapped[str] = mapped_column(String(32), default="Organization")
    plan_tier: Mapped[str] = mapped_column(String(32), default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_login: Mapped[str] = mapped_column(String(128), index=True)
    github_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserOAuthToken(Base):
    __tablename__ = "user_oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(256), default="read:org")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AccountUser(Base):
    __tablename__ = "account_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    authorized: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("account_id", "user_id", name="uq_account_user"),
    )


class AccountRepo(Base):
    __tablename__ = "account_repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    repo_name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("account_id", "repo_name", name="uq_account_repo"),
    )


