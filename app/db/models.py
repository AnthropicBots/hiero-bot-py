# app/db/models.py — ORM models

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Float, Boolean, DateTime, JSON, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(64), default="hiero-bot")
    target_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_login: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_audit_owner_repo", "owner", "repo"),
    )


class PRHealthScore(Base):
    __tablename__ = "pr_health_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    label_applied: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_pr_health_owner_repo", "owner", "repo"),
    )


class ContributorSnapshot(Base):
    __tablename__ = "contributor_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(128), index=True)
    login: Mapped[str] = mapped_column(String(128), index=True)
    merged_prs: Mapped[int] = mapped_column(Integer, default=0)
    reviews_given: Mapped[int] = mapped_column(Integer, default=0)
    months_active: Mapped[int] = mapped_column(Integer, default=0)
    current_role: Mapped[str] = mapped_column(String(32), default="contributor")
    eligible_for: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class StaleActionLog(Base):
    __tablename__ = "stale_action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    owner: Mapped[str] = mapped_column(String(128))
    repo: Mapped[str] = mapped_column(String(128))
    pr_number: Mapped[int] = mapped_column(Integer)
    recommended_reviewer: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    was_assigned: Mapped[bool] = mapped_column(Boolean, default=False)
