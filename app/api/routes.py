# app/api/routes.py — Public REST API

from __future__ import annotations
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.db.database import get_db
from app.db.models import AuditLog, PRHealthScore, ContributorSnapshot, StaleActionLog
from app.utils.logger import get_logger

log = get_logger("api.routes")
router = APIRouter(prefix="/api/v1", tags=["API"])


# ── Response schemas ──────────────────────────────────────────

class AuditEntry(BaseModel):
    id: int
    timestamp: datetime
    action: str
    owner: str
    repo: str
    actor: str
    target_number: Optional[int]
    target_login: Optional[str]
    reason: str
    metadata: Optional[dict]

    model_config = {"from_attributes": True}


class PRHealthEntry(BaseModel):
    id: int
    created_at: datetime
    owner: str
    repo: str
    pr_number: int
    pr_author: str
    score: float
    has_tests: bool
    has_linked_issue: bool
    has_description: bool
    dco_signed: bool
    review_count: int
    files_changed: int
    label_applied: Optional[str]

    model_config = {"from_attributes": True}


class ContributorEntry(BaseModel):
    id: int
    recorded_at: datetime
    owner: str
    repo: str
    login: str
    merged_prs: int
    reviews_given: int
    months_active: int
    current_role: str
    eligible_for: Optional[str]

    model_config = {"from_attributes": True}


class RepoStats(BaseModel):
    owner: str
    repo: str
    avg_pr_health_score: float
    total_prs_scored: int
    total_stale_marked: int
    total_stale_closed: int
    total_auto_unassigned: int
    total_contributors_welcomed: int
    total_audit_events: int


# ── Health ────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "service": "hiero-maintainer-bot"}


# ── Audit log ─────────────────────────────────────────────────

@router.get("/audit", response_model=list[AuditEntry])
async def get_audit_log(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    target_login: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog).order_by(desc(AuditLog.timestamp))
    if owner:
        q = q.where(AuditLog.owner == owner)
    if repo:
        q = q.where(AuditLog.repo == repo)
    if action:
        q = q.where(AuditLog.action == action)
    if target_login:
        q = q.where(AuditLog.target_login == target_login)
    if since:
        q = q.where(AuditLog.timestamp >= since)
    q = q.limit(limit).offset(offset)

    result = await db.execute(q)
    rows = result.scalars().all()
    return [
        AuditEntry(
            id=r.id, timestamp=r.timestamp, action=r.action,
            owner=r.owner, repo=r.repo, actor=r.actor,
            target_number=r.target_number, target_login=r.target_login,
            reason=r.reason, metadata=r.metadata_json,
        )
        for r in rows
    ]


# ── PR Health ─────────────────────────────────────────────────

@router.get("/pr-health", response_model=list[PRHealthEntry])
async def get_pr_health(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    pr_author: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None),
    max_score: Optional[float] = Query(None),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = select(PRHealthScore).order_by(desc(PRHealthScore.created_at))
    if owner:
        q = q.where(PRHealthScore.owner == owner)
    if repo:
        q = q.where(PRHealthScore.repo == repo)
    if pr_author:
        q = q.where(PRHealthScore.pr_author == pr_author)
    if min_score is not None:
        q = q.where(PRHealthScore.score >= min_score)
    if max_score is not None:
        q = q.where(PRHealthScore.score <= max_score)
    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/pr-health/stats")
async def get_pr_health_stats(
    owner: str = Query(...),
    repo: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            func.avg(PRHealthScore.score).label("avg_score"),
            func.count(PRHealthScore.id).label("total"),
            func.min(PRHealthScore.score).label("min_score"),
            func.max(PRHealthScore.score).label("max_score"),
        ).where(PRHealthScore.owner == owner, PRHealthScore.repo == repo)
    )
    row = result.one()
    return {
        "owner": owner,
        "repo": repo,
        "avg_score": round(float(row.avg_score or 0), 1),
        "total_prs": row.total,
        "min_score": round(float(row.min_score or 0), 1),
        "max_score": round(float(row.max_score or 0), 1),
    }


# ── Contributors ──────────────────────────────────────────────

@router.get("/contributors", response_model=list[ContributorEntry])
async def get_contributors(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    login: Optional[str] = Query(None),
    eligible_for: Optional[str] = Query(None),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = select(ContributorSnapshot).order_by(desc(ContributorSnapshot.recorded_at))
    if owner:
        q = q.where(ContributorSnapshot.owner == owner)
    if repo:
        q = q.where(ContributorSnapshot.repo == repo)
    if login:
        q = q.where(ContributorSnapshot.login == login)
    if eligible_for:
        q = q.where(ContributorSnapshot.eligible_for == eligible_for)
    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


# ── Repo stats ────────────────────────────────────────────────

@router.get("/repos/stats", response_model=RepoStats)
async def get_repo_stats(
    owner: str = Query(...),
    repo: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    async def count(model, **filters):
        q = select(func.count()).select_from(model)
        for col, val in filters.items():
            q = q.where(getattr(model, col) == val)
        r = await db.execute(q)
        return r.scalar() or 0

    avg_result = await db.execute(
        select(func.avg(PRHealthScore.score))
        .where(PRHealthScore.owner == owner, PRHealthScore.repo == repo)
    )
    avg_score = float(avg_result.scalar() or 0)

    return RepoStats(
        owner=owner,
        repo=repo,
        avg_pr_health_score=round(avg_score, 1),
        total_prs_scored=await count(PRHealthScore, owner=owner, repo=repo),
        total_stale_marked=await count(StaleActionLog, owner=owner, repo=repo, action="marked_stale"),
        total_stale_closed=await count(StaleActionLog, owner=owner, repo=repo, action="closed"),
        total_auto_unassigned=await count(StaleActionLog, owner=owner, repo=repo, action="unassigned"),
        total_contributors_welcomed=await count(AuditLog, owner=owner, repo=repo, action="contributor.welcomed"),
        total_audit_events=await count(AuditLog, owner=owner, repo=repo),
    )


# ── Stale log ─────────────────────────────────────────────────

@router.get("/stale-log")
async def get_stale_log(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
):
    q = select(StaleActionLog).order_by(desc(StaleActionLog.occurred_at))
    if owner:
        q = q.where(StaleActionLog.owner == owner)
    if repo:
        q = q.where(StaleActionLog.repo == repo)
    if action:
        q = q.where(StaleActionLog.action == action)
    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()
