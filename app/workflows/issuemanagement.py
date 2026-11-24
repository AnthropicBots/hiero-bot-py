# app/workflows/issuemanagement.py

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.schema import IssueManagementConfig
from app.db.models import PendingIssueCloseNotice, StaleActionLog
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("workflow.issuemanagement")

# Every lifecycle comment the bot posts (stale / unassigned / closed) ends with a
# hidden marker that records the issue's last genuine activity *before* the bot
# acted. GitHub bumps ``updated_at`` for the bot's own label, unassignment and
# comment, so without this record the next scan could not tell the bot's
# housekeeping from real activity and the inactivity countdown would restart.
#
# The marker must be the very end of the comment. The bot also echoes user-written
# text (an issue title in the label-escalation comment), and a marker smuggled in
# through that text is never at the end.
_MARKER_RE = re.compile(
    r"<!--\s*hiero-bot:lifecycle:(?P<action>stale|unassigned|closed)\s+"
    r"last_activity=(?P<last_activity>\S+?)\s*-->\s*\Z"
)

# Comments posted before markers existed. Like the marker itself, honoured only
# when written by a bot, so a contributor quoting the text still counts as activity.
_LEGACY_PHRASES = {
    "stale": "marked **stale** due to inactivity",
    "unassigned": "auto-unassigned due to inactivity",
    "closed": "automatically closed** due to continued inactivity",
}

# Every bot action ends with its lifecycle comment, so the ``updated_at`` GitHub
# stamps for that action is the comment's own time. An ``updated_at`` this close
# to the bot's last comment is its own; anything later is somebody else's.
_BOT_ACTION_TOLERANCE = timedelta(seconds=60)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_bot(user: dict | None) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def _is_own_comment(comment: dict) -> bool:
    """True only when GitHub identifies this comment as ours.

    A bot username alone is not sufficient: another GitHub App or bot
    could otherwise forge a lifecycle marker. Missing or incomplete App
    metadata must fail closed.
    """
    if not _is_bot(comment.get("user")):
        return False

    configured_app_id = str(settings.github_app_id or "").strip()
    if not configured_app_id:
        return False

    app = comment.get("performed_via_github_app")
    if not isinstance(app, dict) or app.get("id") is None:
        return False

    return str(app["id"]) == configured_app_id


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
        # independent: close_stale_after_days is additional time counted from
        # the moment the issue is marked stale.
        stale_cutoff = now - timedelta(days=cfg.stale_issue_days)
        unassign_cutoff = now - timedelta(
            days=cfg.auto_unassign_inactive_days
        )

        counts = {"stale_marked": 0, "closed": 0, "unassigned": 0, "errors": 0}

        # Recover unfinished close operations before scanning open issues.
        # This is necessary because successfully closed issues no longer
        # appear in the normal open-issue listing.
        pending_issue_numbers: set[int] = set()
        try:
            pending_issue_numbers = await self._recover_pending_close_notices(
                ctx, now, counts
            )
        except Exception as exc:
            counts["errors"] += 1
            log.warning(
                "Stale scan %s/%s: pending close-notice recovery failed: %s",
                owner, repo, exc,
            )
            await db.rollback()

        issues = await self._gh.list_issues(
            owner, repo, inst, state="open", sort="updated", direction="asc"
        )

        cutoffs = (stale_cutoff, unassign_cutoff)

        for issue in issues:
            # Pending closures are handled exclusively by recovery above.
            if issue.get("number") in pending_issue_numbers:
                continue

            # One failing issue must not abort the scan or discard audit rows
            # for actions already taken on other issues.
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

        Genuine activity comes from contributor comments and human-authored
        GitHub timeline events. Other bots' comments are configurable.

        Lifecycle comments record the activity baseline from before the bot
        acted. When an action succeeds but its notification fails, the
        attributable GitHub App timeline event provides a safe fallback
        baseline instead.
        """
        comments = await self._gh.list_issue_comments(
            ctx["owner"], ctx["repo"], issue["number"], ctx["installation_id"]
        )
        timeline_events = await self._gh.list_issue_timeline(
            ctx["owner"], ctx["repo"], issue["number"], ctx["installation_id"]
        )

        human_last: datetime | None = None
        bot_last: datetime | None = None
        bot_baseline: datetime | None = None
        bot_event_last: datetime | None = None
        bot_stale_marked_at: datetime | None = None
        stale_marked_at: datetime | None = None
        ignored_bot_activity = False
        # Latest moment any bot (ours or another) touched the issue. GitHub bumps
        # ``updated_at`` for each of those, so only an ``updated_at`` clearly later
        # than this can be a person's edit, which leaves no comment or event.
        bot_activity_last: datetime | None = None

        cfg = ctx["config"].workflows.issue_management

        for comment in comments:
            created_at = comment.get("created_at")
            if not created_at:
                continue

            timestamp = _parse_timestamp(created_at)
            action, baseline = self._classify_comment(comment)

            if action is None:
                user = comment.get("user") or {}
                is_bot = _is_bot(user)
                is_our_bot = _is_own_comment(comment)

                if is_our_bot:
                    # An ordinary comment from our own App is not a
                    # lifecycle marker. Count its actual timestamp so
                    # marker-like text in an echoed title cannot alter
                    # the activity baseline.
                    if human_last is None or timestamp > human_last:
                        human_last = timestamp

                    continue

                if is_bot and not cfg.count_bot_comments_as_activity:
                    ignored_bot_activity = True
                    if bot_activity_last is None or timestamp > bot_activity_last:
                        bot_activity_last = timestamp
                    continue

                if human_last is None or timestamp > human_last:
                    human_last = timestamp

                continue

            if baseline is not None:
                baseline = min(baseline, timestamp)

            if bot_activity_last is None or timestamp > bot_activity_last:
                bot_activity_last = timestamp

            if bot_last is None or timestamp >= bot_last:
                bot_last, bot_baseline = timestamp, baseline

            if action == "stale" and (
                stale_marked_at is None or timestamp > stale_marked_at
            ):
                stale_marked_at = timestamp

        activity_events = {
            "assigned",
            "unassigned",
            "labeled",
            "unlabeled",
            "milestoned",
            "demilestoned",
            "renamed",
            "reopened",
            "locked",
            "unlocked",
        }

        configured_app_id = str(settings.github_app_id or "").strip()

        for event in timeline_events:
            event_name = event.get("event")
            if event_name not in activity_events:
                continue

            actor = event.get("actor") or {}
            created_at = event.get("created_at")
            if not created_at:
                continue

            timestamp = _parse_timestamp(created_at)

            if actor and not _is_bot(actor):
                if human_last is None or timestamp > human_last:
                    human_last = timestamp
                continue

            if bot_activity_last is None or timestamp > bot_activity_last:
                bot_activity_last = timestamp

            app = event.get("performed_via_github_app")
            is_own_app_event = (
                bool(configured_app_id)
                and _is_bot(actor)
                and isinstance(app, dict)
                and app.get("id") is not None
                and str(app["id"]) == configured_app_id
            )

            if is_own_app_event:
                if event_name == "unassigned":
                    # If the unassignment notice failed, retain the
                    # attributable lifecycle event as a safe fallback.
                    if bot_event_last is None or timestamp > bot_event_last:
                        bot_event_last = timestamp

                elif event_name == "labeled":
                    label = event.get("label") or {}
                    label_name = (
                        label.get("name")
                        if isinstance(label, dict)
                        else label
                    )

                    if (
                        isinstance(label_name, str)
                        and label_name.casefold() == cfg.stale_label.casefold()
                    ):
                        if bot_event_last is None or timestamp > bot_event_last:
                            bot_event_last = timestamp

                        if (
                            bot_stale_marked_at is None
                            or timestamp > bot_stale_marked_at
                        ):
                            bot_stale_marked_at = timestamp
                    else:
                        # Unrelated labels are not genuine issue activity.
                        ignored_bot_activity = True

                else:
                    # Other events generated by our App must not make
                    # updated_at look like human activity.
                    ignored_bot_activity = True

            elif actor and _is_bot(actor):
                ignored_bot_activity = True

        # A lifecycle comment is the strongest record because its marker
        # carries the pre-action activity baseline. If its comment failed,
        # use the attributable bot timeline event instead.
        if bot_last is not None:
            candidates = [human_last, bot_baseline or bot_last]
        elif bot_event_last is not None:
            candidates = [human_last, bot_event_last]
        elif ignored_bot_activity:
            # Excluded bot activity can bump updated_at. Do not mistake that
            # bump for contributor activity.
            created_at = issue.get("created_at")
            if created_at:
                candidates = [human_last, _parse_timestamp(created_at)]
            else:
                candidates = [
                    human_last,
                    datetime.min.replace(tzinfo=UTC),
                ]
        else:
            candidates = [human_last, updated]

        # An edit leaves neither a comment nor a timeline event; its only trace is
        # ``updated_at``. Count it once it is clearly later than every bot action.
        if bot_activity_last is not None and updated > bot_activity_last + _BOT_ACTION_TOLERANCE:
            candidates.append(updated)

        valid_candidates = [candidate for candidate in candidates if candidate]
        last_activity = max(valid_candidates)

        if stale_marked_at is None:
            stale_marked_at = bot_stale_marked_at

        return last_activity, stale_marked_at

    @staticmethod
    def _classify_comment(comment: dict) -> tuple[str | None, datetime | None]:
        """Return ``(action, recorded_activity)`` for a bot lifecycle comment,
        or ``(None, None)`` for anything else (which counts as activity).

        Only comments written by a bot are trusted: the marker is plain text, so
        honouring it from anyone could let a contributor forge a far-future
        ``last_activity`` and keep an issue from ever going stale.
        """
        if not _is_own_comment(comment):
            return None, None

        body = comment.get("body") or ""

        match = _MARKER_RE.search(body)
        if match:
            try:
                recorded = _parse_timestamp(match["last_activity"])
            except ValueError:
                recorded = None
            return match["action"], recorded

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
        # A failure here must not stop the stale decision below: the two actions
        # are independent and both are judged from the same pre-scan activity.
        if assignees and last_activity < unassign_cutoff:
            try:
                await self._unassign_inactive(
                    ctx, issue, assignees, days_inactive, last_activity
                )
                counts["unassigned"] += len(assignees)
            except Exception as exc:
                counts["errors"] += 1
                log.warning(
                    "Stale scan %s/%s: could not unassign on #%s: %s",
                    ctx["owner"], ctx["repo"], issue["number"], exc,
                )

        # Close stale: the close period runs from the stale marking, and is
        # pushed back by any genuine activity since.
        if is_stale:
            close_reference = max(stale_marked_at or last_activity, last_activity)
            if now >= close_reference + timedelta(days=cfg.close_stale_after_days):
                newly_closed = await self._close_stale(
                    ctx, issue, days_inactive, last_activity
                )
                if newly_closed:
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

        # The comment goes last: GitHub stamps updated_at when it lands, so the
        # bump that follows is the comment's own and the marker explains it. If
        # the comment fails the error is counted and the scan moves on: the label
        # stays, so the issue is stale from the next scan (its close period runs
        # from the label event) and the missing notice is not retried.
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

    async def _recover_pending_close_notices(
        self,
        ctx: dict,
        now: datetime,
        counts: dict[str, int],
    ) -> set[int]:
        """Retry pending close operations and their notifications."""
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]

        result = await db.execute(
            select(PendingIssueCloseNotice.issue_number).where(
                PendingIssueCloseNotice.owner == owner,
                PendingIssueCloseNotice.repo == repo,
            )
        )
        pending_numbers = set(result.scalars().all())

        result = await db.execute(
            select(PendingIssueCloseNotice.id).where(
                PendingIssueCloseNotice.owner == owner,
                PendingIssueCloseNotice.repo == repo,
            )
        )
        pending_ids = list(result.scalars().all())

        for pending_id in pending_ids:
            try:
                result = await db.execute(
                    select(PendingIssueCloseNotice).where(
                        PendingIssueCloseNotice.id == pending_id
                    )
                )
                pending = result.scalar_one_or_none()

                if pending is None:
                    continue

                number = pending.issue_number
                pending_created_at = pending.created_at

                issue = await self._gh.get_issue(
                    owner, repo, number, inst
                )
                timeline = await self._gh.list_issue_timeline(
                    owner, repo, number, inst
                )

                # A human reopening or closing the issue after the pending
                # operation was created takes precedence over automation.
                human_intervened = any(
                    event.get("event") in {"reopened", "closed"}
                    and event.get("created_at")
                    and event.get("actor")
                    and not _is_bot(event["actor"])
                    and _parse_timestamp(event["created_at"])
                    > pending_created_at
                    for event in timeline
                )

                if human_intervened:
                    await db.delete(pending)
                    await db.commit()
                    log.info(
                        "Stale scan %s/%s: cancelled pending close for "
                        "issue #%s after human intervention",
                        owner, repo, number,
                    )
                    continue

                issue_state = (issue.get("state") or "").casefold()

                if issue_state == "open":
                    updated_at = issue.get("updated_at") or issue.get(
                        "created_at"
                    )
                    updated = (
                        _parse_timestamp(updated_at)
                        if updated_at
                        else datetime.min.replace(tzinfo=UTC)
                    )
                    last_activity, _ = await self._get_last_activity(
                        ctx, issue, updated
                    )

                    if last_activity > pending_created_at:
                        await db.delete(pending)
                        await db.commit()
                        log.info(
                            "Stale scan %s/%s: cancelled pending close for "
                            "issue #%s after new activity",
                            owner, repo, number,
                        )
                        continue

                    await self._gh.close_issue(
                        owner, repo, number, inst
                    )

                    pending.closed_at = now
                    await self._log_stale(
                        ctx, number, "closed", pending.days_inactive
                    )
                    await audit.record(
                        db,
                        action="issue.closed",
                        owner=owner,
                        repo=repo,
                        target_number=number,
                        reason="Closed after stale period expired",
                    )
                    await db.commit()
                    counts["closed"] += 1

                elif issue_state == "closed" and pending.closed_at is None:
                    # GitHub may have accepted the original close request
                    # even if the client timed out. Only attribute that
                    # closure to this bot when the timeline confirms it.
                    configured_app_id = str(
                        settings.github_app_id or ""
                    ).strip()

                    own_close_event = bool(configured_app_id) and any(
                        event.get("event") == "closed"
                        and event.get("created_at")
                        and _is_bot(event.get("actor"))
                        and isinstance(
                            event.get("performed_via_github_app"), dict
                        )
                        and event["performed_via_github_app"].get("id")
                        is not None
                        and str(
                            event["performed_via_github_app"]["id"]
                        ) == configured_app_id
                        and _parse_timestamp(event["created_at"])
                        >= pending_created_at
                        for event in timeline
                    )

                    if not own_close_event:
                        raise RuntimeError(
                            f"Issue #{number} is closed, but its closure "
                            "cannot be attributed to this GitHub App"
                        )

                    pending.closed_at = now
                    await self._log_stale(
                        ctx, number, "closed", pending.days_inactive
                    )
                    await audit.record(
                        db,
                        action="issue.closed",
                        owner=owner,
                        repo=repo,
                        target_number=number,
                        reason="Closed after stale period expired",
                    )
                    await db.commit()
                    counts["closed"] += 1

                elif issue_state != "closed":
                    raise RuntimeError(
                        f"Unexpected state for issue #{number}: "
                        f"{issue_state!r}"
                    )

                await self._deliver_close_notice(ctx, pending)

            except Exception as exc:
                counts["errors"] += 1
                log.warning(
                    "Stale scan %s/%s: could not recover close notice "
                    "for pending record #%s: %s",
                    owner, repo, pending_id, exc,
                )
                await db.rollback()

        return pending_numbers

    async def _deliver_close_notice(
        self,
        ctx: dict,
        pending: PendingIssueCloseNotice,
    ) -> None:
        """Deliver a pending close notice once, then remove its outbox record."""
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = pending.issue_number
        db: AsyncSession = ctx["db"]

        notice_marker = f"<!-- hiero-bot:close-notice:{pending.id} -->"

        comments = await self._gh.list_issue_comments(
            owner, repo, number, inst
        )

        already_notified = any(
            notice_marker in (comment.get("body") or "")
            and _is_own_comment(comment)
            for comment in comments
        )

        if not already_notified:
            await self._gh.post_comment(
                owner,
                repo,
                number,
                (
                    "🔒 This issue has been **automatically closed** due to "
                    "continued inactivity.\n\n"
                    "Feel free to re-open if still relevant."
                    + _marker("closed", pending.last_activity)
                    + f"\n\n{notice_marker}"
                ),
                inst,
            )

        # Keep the record if posting raises. A later scan can check whether
        # GitHub accepted the comment before retrying it.
        await db.delete(pending)
        await db.commit()

    async def _close_stale(
        self,
        ctx: dict,
        issue: dict,
        days_inactive: int = 0,
        last_activity: datetime | None = None,
    ) -> bool:
        """Close an issue and reliably deliver its close notification.

        The pending record is committed before the GitHub close request.
        If closing or commenting fails, the record remains available for
        recovery by a later stale scan.
        """
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]
        db: AsyncSession = ctx["db"]

        result = await db.execute(
            select(PendingIssueCloseNotice).where(
                PendingIssueCloseNotice.owner == owner,
                PendingIssueCloseNotice.repo == repo,
                PendingIssueCloseNotice.issue_number == number,
            )
        )
        pending = result.scalar_one_or_none()

        if pending is None:
            pending = PendingIssueCloseNotice(
                owner=owner,
                repo=repo,
                issue_number=number,
                days_inactive=days_inactive,
                last_activity=last_activity,
            )
            db.add(pending)

            # This commit must happen before the external GitHub operation.
            await db.commit()

        newly_closed = pending.closed_at is None

        if newly_closed:
            await self._gh.close_issue(owner, repo, number, inst)

            pending.closed_at = datetime.now(UTC)

            await self._log_stale(
                ctx, number, "closed", pending.days_inactive
            )
            await audit.record(
                db,
                action="issue.closed",
                owner=owner,
                repo=repo,
                target_number=number,
                reason="Closed after stale period expired",
            )

            # Persist closure and its audit record before attempting the
            # notification. A notification failure must not lose either.
            await db.commit()

        await self._deliver_close_notice(ctx, pending)

        return newly_closed

    async def _unassign_inactive(
        self, ctx: dict, issue: dict, assignees: list[str], days_inactive: int = 0,
        last_activity: datetime | None = None,
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]
        mentions = " ".join(f"@{a}" for a in assignees)

        # Remove first, comment last (see _mark_stale). A failed removal posts
        # nothing, so retries on later scans cannot pile up duplicate notices.
        await self._gh.remove_assignees(owner, repo, number, assignees, inst)
        try:
            await self._gh.post_comment(
                owner, repo, number,
                f"⏰ {mentions} — auto-unassigned due to inactivity. "
                f"Re-assign yourself with `/assign` if you're still working on this!"
                + _marker("unassigned", last_activity),
                inst,
            )
        except Exception as exc:
            # The unassignment happened and must be recorded; only the notice is
            # lost. Do not retry: the issue has no assignee left to scan again.
            log.warning(
                "Unassigned %s on %s/%s#%s but the notice failed: %s",
                mentions, owner, repo, number, exc,
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
