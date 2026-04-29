# app/utils/audit.py — Persistent audit trail

from __future__ import annotations
from datetime import datetime
from typing import Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import AuditLog
from app.utils.logger import get_logger

log = get_logger("audit")

VALID_ACTIONS = {
    "issue.assigned", "issue.unassigned", "issue.closed", "issue.labeled",
    "issue.stale_marked", "pr.reviewed", "pr.health_scored", "pr.labeled",
    "pr.closed_stale", "pr.reviewer_recommended",
    "contributor.mentor_assigned", "contributor.role_suggested",
    "contributor.welcomed", "workflow.skipped", "workflow.error",
}


async def record(
    db: AsyncSession,
    *,
    action: str,
    owner: str,
    repo: str,
    reason: str,
    target_number: Optional[int] = None,
    target_login: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> AuditLog:
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")

    entry = AuditLog(
        timestamp=datetime.utcnow(),
        action=action,
        owner=owner,
        repo=repo,
        reason=reason,
        target_number=target_number,
        target_login=target_login,
        metadata_json=metadata,
    )
    db.add(entry)
    await db.flush()

    log.info("[audit] %s %s/%s #%s @%s — %s",
             action, owner, repo,
             target_number or "-",
             target_login or "-",
             reason)
    return entry
