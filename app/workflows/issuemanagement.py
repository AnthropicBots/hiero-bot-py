# app/workflows/issuemanagement.py

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.github.client import GitHubClient
from app.db.models import StaleActionLog
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.issuemanagement")


class IssueManagementWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    # ── Scheduled stale scan ──────────────────────────────────

    async def run_stale_scan(self, ctx: dict) -> dict[str, int]:
        cfg = ctx["config"].workflows.issue_management
        if not cfg.enabled:
            return {}

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        now = datetime.now(timezone.utc)

        stale_cutoff = now - timedelta(days=cfg.stale_issue_days)
        close_cutoff = now - timedelta(days=cfg.stale_issue_days + cfg.close_stale_after_days)
        unassign_cutoff = now - timedelta(days=cfg.auto_unassign_inactive_days)

        issues = await self._gh.list_issues(
            owner, repo, inst, state="open", sort="updated", direction="asc"
        )

        counts = {"stale_marked": 0, "closed": 0, "unassigned": 0}

        for issue in issues:
            if issue.get("pull_request"):
                continue  # Skip PRs

            labels = [
                (lb["name"] if isinstance(lb, dict) else lb)
                for lb in issue.get("labels", [])
            ]
            if any(el in labels for el in cfg.exempt_labels):
                continue

            updated = datetime.fromisoformat(
                issue["updated_at"].replace("Z", "+00:00")
            )
            is_stale = cfg.stale_label in labels

            # Auto-unassign
            assignees = [a["login"] for a in (issue.get("assignees") or []) if a]
            if assignees and updated < unassign_cutoff:
                await self._unassign_inactive(ctx, issue, assignees)
                counts["unassigned"] += len(assignees)

            # Close stale
            if is_stale and updated < close_cutoff:
                await self._close_stale(ctx, issue)
                counts["closed"] += 1
                continue

            # Mark stale
            if not is_stale and updated < stale_cutoff:
                await self._mark_stale(ctx, issue, cfg.stale_label)
                counts["stale_marked"] += 1

        await db.commit()
        log.info("Stale scan %s/%s: %s", owner, repo, counts)
        return counts

    # ── Label escalation ─────────────────────────────────────

    async def handle_label_escalation(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.issue_management
        if not cfg.enabled:
            return

        label_name = (payload.get("label") or {}).get("name", "")
        issue = payload.get("issue") or {}
        issue_number = issue.get("number")
        if not label_name or not issue_number:
            return

        rule = next((r for r in cfg.label_escalation_rules if r.label == label_name), None)
        if not rule:
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        mention = f"@{owner}/{rule.notify_team}"
        title = issue.get("title", "")

        await self._gh.post_comment(
            owner, repo, issue_number,
            f"🔔 {mention} — issue labeled `{label_name}` requires your attention.\n\n> **{title}**",
            inst,
        )
        await audit.record(
            ctx["db"], action="issue.labeled", owner=owner, repo=repo,
            target_number=issue_number,
            reason=f"Label escalation: {label_name} → {rule.notify_team}",
            metadata={"label": label_name, "team": rule.notify_team},
        )
        await ctx["db"].commit()

    # ── Private helpers ───────────────────────────────────────

    async def _mark_stale(self, ctx: dict, issue: dict, stale_label: str) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]

        await self._gh.add_label(owner, repo, number, stale_label, inst)
        await self._gh.post_comment(
            owner, repo, number,
            f"⚠️ This issue has been marked **stale** due to inactivity.\n\n"
            f"If it's still relevant please comment to keep it open. "
            f"Remove the `{stale_label}` label to exempt it permanently.",
            inst,
        )

        await self._log_stale(ctx, number, "marked_stale")
        await audit.record(
            ctx["db"], action="issue.stale_marked", owner=owner, repo=repo,
            target_number=number, reason="No activity within stale period",
        )

    async def _close_stale(self, ctx: dict, issue: dict) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]

        await self._gh.post_comment(
            owner, repo, number,
            "🔒 This issue has been **automatically closed** due to continued inactivity.\n\n"
            "Feel free to re-open if still relevant.",
            inst,
        )
        await self._gh.close_issue(owner, repo, number, inst)
        await self._log_stale(ctx, number, "closed")
        await audit.record(
            ctx["db"], action="issue.closed", owner=owner, repo=repo,
            target_number=number, reason="Closed after stale period expired",
        )

    async def _unassign_inactive(self, ctx: dict, issue: dict, assignees: list[str]) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]
        mentions = " ".join(f"@{a}" for a in assignees)

        await self._gh.remove_assignees(owner, repo, number, assignees, inst)
        await self._gh.post_comment(
            owner, repo, number,
            f"⏰ {mentions} — auto-unassigned due to inactivity. "
            f"Re-assign yourself with `/assign` if you're still working on this!",
            inst,
        )
        await self._log_stale(ctx, number, "unassigned")
        for login in assignees:
            await audit.record(
                ctx["db"], action="issue.unassigned", owner=owner, repo=repo,
                target_number=number, target_login=login,
                reason="Auto-unassigned due to inactivity",
            )

    async def _log_stale(self, ctx: dict, issue_number: int, action: str) -> None:
        updated_str = ""
        days_inactive = 0
        try:
            issue_data = await self._gh.get(
                f"/repos/{ctx['owner']}/{ctx['repo']}/issues/{issue_number}",
                ctx["installation_id"],
            )
            updated = datetime.fromisoformat(
                issue_data["updated_at"].replace("Z", "+00:00")
            )
            days_inactive = (datetime.now(timezone.utc) - updated).days
        except Exception:
            pass

        record = StaleActionLog(
            owner=ctx["owner"],
            repo=ctx["repo"],
            issue_number=issue_number,
            action=action,
            days_inactive=days_inactive,
        )
        ctx["db"].add(record)
