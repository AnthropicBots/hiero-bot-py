# app/workflows/issuemanagement.py

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.schema import IssueManagementConfig
from app.db.models import StaleActionLog
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.issuemanagement")

# Every lifecycle comment the bot posts (stale / unassigned / closed) ends with a
# hidden marker that records the issue's last genuine activity *before* the bot
# acted. GitHub bumps ``updated_at`` for the bot's own label, unassignment and
# comment, so without this record the next scan could not tell the bot's
# housekeeping from real activity and the inactivity countdown would restart.
_MARKER_RE = re.compile(
    r"<!--\s*hiero-bot:lifecycle:(?P<action>[a-z_]+)\s+"
    r"last_activity=(?P<last_activity>\S+?)\s*-->"
)

# Comments posted before markers existed. Honoured only when written by a bot, so
# a contributor quoting the text still counts as activity.
_LEGACY_PHRASES = {
    "stale": "marked **stale** due to inactivity",
    "unassigned": "auto-unassigned due to inactivity",
    "closed": "automatically closed** due to continued inactivity",
}

# The label, unassignment and comment of one bot action land within moments of
# each other; an ``updated_at`` this close to the bot's last comment is its own.
_BOT_ACTION_TOLERANCE = timedelta(minutes=1)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_bot(user: dict | None) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def _marker(action: str, last_activity: datetime | None) -> str:
    if last_activity is None:
        return ""
    return f"\n\n<!-- hiero-bot:lifecycle:{action} last_activity={last_activity.isoformat()} -->"


class IssueManagementWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    # ── Scheduled stale scan ──────────────────────────────────

    async def run_stale_scan(
        self, ctx: dict, now: datetime | None = None
    ) -> dict[str, int]:
        cfg = ctx["config"].workflows.issue_management
        if not cfg.enabled:
            return {}

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        now = now or datetime.now(UTC)

        # Every threshold comes from the repository's configuration. They are
        # independent: close_stale_after_days is *additional* time counted from
        # the moment the issue is marked stale, so it may be shorter or longer
        # than stale_issue_days.
        stale_cutoff = now - timedelta(days=cfg.stale_issue_days)
        unassign_cutoff = now - timedelta(days=cfg.auto_unassign_inactive_days)

        issues = await self._gh.list_issues(
            owner, repo, inst, state="open", sort="updated", direction="asc"
        )

        counts = {"stale_marked": 0, "closed": 0, "unassigned": 0, "errors": 0}
        cutoffs = (stale_cutoff, unassign_cutoff)

        for issue in issues:
            # One failing issue (a 403 secondary rate limit, a deleted issue, a
            # network error) must not abort the scan or discard the audit rows
            # of actions already taken, so each issue is isolated and committed
            # on its own.
            try:
                await self._scan_issue(ctx, cfg, issue, now, cutoffs, counts)
                await db.commit()
            except Exception as exc:
                counts["errors"] += 1
                number = issue.get("number") if isinstance(issue, dict) else None
                log.warning(
                    "Stale scan %s/%s: skipping issue #%s after error: %s",
                    owner, repo, number, exc,
                )
                await db.rollback()

        log.info("Stale scan %s/%s: %s", owner, repo, counts)
        return counts

    async def _get_last_activity(
        self, ctx: dict, issue: dict, updated: datetime
    ) -> tuple[datetime, datetime | None]:
        """Return ``(last_activity, stale_marked_at)`` for one issue.

        ``updated_at`` cannot be trusted on its own: the bot's own housekeeping
        bumps it. Genuine activity is therefore the latest of

        * contributor comments,
        * the activity the bot recorded in its lifecycle comments (the state
          *before* it acted), and
        * ``updated_at`` -- but only when it is newer than the bot's last
          comment, i.e. when somebody other than the bot touched the issue
          (an edit, a label, a re-assignment).
        """
        comments = await self._gh.list_issue_comments(
            ctx["owner"], ctx["repo"], issue["number"], ctx["installation_id"]
        )

        human_last: datetime | None = None
        bot_last: datetime | None = None
        bot_baseline: datetime | None = None
        stale_marked_at: datetime | None = None

        for comment in comments:
            created_at = comment.get("created_at")
            if not created_at:
                continue
            timestamp = _parse_timestamp(created_at)
            action, baseline = self._classify_comment(comment)

            if action is None:
                if human_last is None or timestamp > human_last:
                    human_last = timestamp
                continue

            if bot_last is None or timestamp >= bot_last:
                bot_last, bot_baseline = timestamp, baseline
            if action == "stale" and (
                stale_marked_at is None or timestamp > stale_marked_at
            ):
                stale_marked_at = timestamp

        candidates = [human_last] if human_last else []
        if bot_last is None or updated > bot_last + _BOT_ACTION_TOLERANCE:
            candidates.append(updated)  # nothing but real activity explains it
        else:
            # The bump is the bot's own. Fall back to the activity it recorded;
            # a comment from before markers existed recorded none, so the time
            # of that comment is the best available estimate.
            candidates.append(bot_baseline or bot_last)

        return max(candidates), stale_marked_at

    @staticmethod
    def _classify_comment(comment: dict) -> tuple[str | None, datetime | None]:
        """Return ``(action, recorded_activity)`` for a bot lifecycle comment,
        or ``(None, None)`` for anything else (which counts as activity)."""
        body = comment.get("body") or ""

        match = _MARKER_RE.search(body)
        if match:
            try:
                recorded = _parse_timestamp(match["last_activity"])
            except ValueError:
                recorded = None
            return match["action"], recorded

        if _is_bot(comment.get("user")):
            folded = body.casefold()
            for action, phrase in _LEGACY_PHRASES.items():
                if phrase in folded:
                    return action, None
        return None, None

    async def _scan_issue(
        self,
        ctx: dict,
        cfg: IssueManagementConfig,
        issue: dict,
        now: datetime,
        cutoffs: tuple[datetime, datetime],
        counts: dict[str, int],
    ) -> None:
        """Apply the stale rules to one issue, updating ``counts`` in place."""
        stale_cutoff, unassign_cutoff = cutoffs

        if issue.get("pull_request"):
            return  # Skip PRs

        # GitHub label names are case-insensitive, so compare them casefolded.
        labels = {
            (lb["name"] if isinstance(lb, dict) else lb).casefold()
            for lb in issue.get("labels", [])
        }
        if labels & {name.casefold() for name in cfg.exempt_labels}:
            return

        updated = _parse_timestamp(issue["updated_at"])
        is_stale = cfg.stale_label.casefold() in labels

        # Decide everything from the activity as it was *before* this scan acts:
        # unassigning and marking stale both post comments, and the second
        # decision must not see the first one's side effects.
        last_activity, stale_marked_at = await self._get_last_activity(
            ctx, issue, updated
        )
        days_inactive = (now - last_activity).days

        assignees = [
            person["login"]
            for person in (issue.get("assignees") or [])
            if person and person.get("login")
        ]

        # Auto-unassign
        if assignees and last_activity < unassign_cutoff:
            await self._unassign_inactive(
                ctx, issue, assignees, days_inactive, last_activity
            )
            counts["unassigned"] += len(assignees)

        # Close stale: the close period runs from the stale marking, and is
        # pushed back by any genuine activity since.
        if is_stale:
            close_reference = max(stale_marked_at or last_activity, last_activity)
            if now >= close_reference + timedelta(days=cfg.close_stale_after_days):
                await self._close_stale(ctx, issue, days_inactive, last_activity)
                counts["closed"] += 1
            return

        # Mark stale (independent of the unassignment above, so an issue past
        # both thresholds gets both actions in the same scan)
        if last_activity < stale_cutoff:
            await self._mark_stale(
                ctx, issue, cfg.stale_label, days_inactive, last_activity
            )
            counts["stale_marked"] += 1

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

    async def _mark_stale(
        self, ctx: dict, issue: dict, stale_label: str, days_inactive: int = 0,
        last_activity: datetime | None = None,
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]

        await self._gh.add_label(owner, repo, number, stale_label, inst)
        await self._gh.post_comment(
            owner, repo, number,
            f"⚠️ This issue has been marked **stale** due to inactivity.\n\n"
            f"If it's still relevant please comment to keep it open. "
            f"Remove the `{stale_label}` label to exempt it permanently."
            + _marker("stale", last_activity),
            inst,
        )

        await self._log_stale(ctx, number, "marked_stale", days_inactive)
        await audit.record(
            ctx["db"], action="issue.stale_marked", owner=owner, repo=repo,
            target_number=number, reason="No activity within stale period",
        )

    async def _close_stale(
        self, ctx: dict, issue: dict, days_inactive: int = 0,
        last_activity: datetime | None = None,
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]

        await self._gh.post_comment(
            owner, repo, number,
            "🔒 This issue has been **automatically closed** due to continued inactivity.\n\n"
            "Feel free to re-open if still relevant."
            + _marker("closed", last_activity),
            inst,
        )
        await self._gh.close_issue(owner, repo, number, inst)
        await self._log_stale(ctx, number, "closed", days_inactive)
        await audit.record(
            ctx["db"], action="issue.closed", owner=owner, repo=repo,
            target_number=number, reason="Closed after stale period expired",
        )

    async def _unassign_inactive(
        self, ctx: dict, issue: dict, assignees: list[str], days_inactive: int = 0,
        last_activity: datetime | None = None,
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]
        mentions = " ".join(f"@{a}" for a in assignees)

        await self._gh.remove_assignees(owner, repo, number, assignees, inst)
        await self._gh.post_comment(
            owner, repo, number,
            f"⏰ {mentions} — auto-unassigned due to inactivity. "
            f"Re-assign yourself with `/assign` if you're still working on this!"
            + _marker("unassigned", last_activity),
            inst,
        )
        await self._log_stale(ctx, number, "unassigned", days_inactive)
        for login in assignees:
            await audit.record(
                ctx["db"], action="issue.unassigned", owner=owner, repo=repo,
                target_number=number, target_login=login,
                reason="Auto-unassigned due to inactivity",
            )

    async def _log_stale(self, ctx: dict, issue_number: int, action: str, days_inactive: int = 0) -> None:
        record = StaleActionLog(
            owner=ctx["owner"],
            repo=ctx["repo"],
            issue_number=issue_number,
            action=action,
            days_inactive=days_inactive,
        )
        ctx["db"].add(record)