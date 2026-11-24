# app/workflows/issuemanagement.py

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.schema import IssueManagementConfig
from app.db.models import LifecycleAction, PendingIssueCloseNotice, StaleActionLog
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

# A notice that keeps failing is given up on after this many delivery attempts.
_MAX_NOTICE_ATTEMPTS = 5

# The update time read back after our own change may already include a person's
# edit made in the meantime, so it is trusted only up to this far past the last
# time GitHub attributes to our own action. An edit within this window of it
# cannot be told apart from our own change.
_SETTLE_TOLERANCE = timedelta(seconds=5)


class _AlreadyClaimed(Exception):
    """Another worker (or an earlier unfinished attempt) owns this action."""


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


def _notice_marker(record_id: int) -> str:
    return f"<!-- hiero-bot:notice:{record_id} -->"


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

        # Settle unfinished unassign / stale actions, then deliver notices that
        # failed on an earlier scan.
        try:
            await self._recover_pending_actions(ctx)
            await self._recover_pending_notices(ctx)
        except Exception as exc:
            counts["errors"] += 1
            log.warning(
                "Stale scan %s/%s: pending notice recovery failed: %s",
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
        result = await ctx["db"].execute(
            select(LifecycleAction)
            .where(
                LifecycleAction.owner == ctx["owner"],
                LifecycleAction.repo == ctx["repo"],
                LifecycleAction.issue_number == issue["number"],
                LifecycleAction.applied_at.is_not(None),
            )
            .order_by(LifecycleAction.created_at, LifecycleAction.id)
        )
        records = list(result.scalars().all())

        human_last: datetime | None = None
        bot_last: datetime | None = None
        bot_baseline: datetime | None = None
        bot_event_last: datetime | None = None
        bot_stale_marked_at: datetime | None = None
        stale_marked_at: datetime | None = None
        ignored_bot_activity = False
        # GitHub bumps ``updated_at`` for every bot action, so only an
        # ``updated_at`` clearly later than all of them can be a person's edit,
        # which leaves no comment or event. Our own actions are recorded with the
        # exact time GitHub stamped (``own_floor``); other bots' are not, so they
        # get a short tolerance window instead.
        own_bot_last: datetime | None = None
        other_bot_last: datetime | None = None

        cfg = ctx["config"].workflows.issue_management

        for comment in comments:
            created_at = comment.get("created_at")
            if not created_at:
                continue

            timestamp = _parse_timestamp(created_at)
            action, baseline = self._classify_comment(comment)

            if action is None:
                # Not a lifecycle comment. Any bot's ordinary comment -- including
                # this app's own escalation or welcome comments -- follows the
                # bot-comment policy; its timestamp is used as is, so marker-like
                # text echoed into it cannot alter the activity baseline.
                is_bot = _is_bot(comment.get("user"))

                if is_bot and not cfg.count_bot_comments_as_activity:
                    ignored_bot_activity = True
                    if other_bot_last is None or timestamp > other_bot_last:
                        other_bot_last = timestamp
                    continue

                if human_last is None or timestamp > human_last:
                    human_last = timestamp

                continue

            if baseline is not None:
                baseline = min(baseline, timestamp)

            if own_bot_last is None or timestamp > own_bot_last:
                own_bot_last = timestamp

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

            app = event.get("performed_via_github_app")
            is_own_app_event = (
                bool(configured_app_id)
                and _is_bot(actor)
                and isinstance(app, dict)
                and app.get("id") is not None
                and str(app["id"]) == configured_app_id
            )

            if is_own_app_event:
                if own_bot_last is None or timestamp > own_bot_last:
                    own_bot_last = timestamp

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
                if other_bot_last is None or timestamp > other_bot_last:
                    other_bot_last = timestamp

        # Our own recorded actions are stored before their notices go out, so
        # they carry the pre-action activity baseline even when the notice (and
        # its marker) never made it to GitHub, and the exact update time GitHub
        # stamped for them.
        own_floor: datetime | None = None
        for record in records:
            if record.settled_updated_at is not None and (
                own_floor is None or record.settled_updated_at > own_floor
            ):
                own_floor = record.settled_updated_at
            record_time = _parse_timestamp(record.created_at.isoformat())
            if bot_last is None or record_time > bot_last:
                bot_last = record_time
                bot_baseline = (
                    _parse_timestamp(record.last_activity.isoformat())
                    if record.last_activity
                    else None
                )
            if record.action == "stale" and (
                stale_marked_at is None or record_time > stale_marked_at
            ):
                stale_marked_at = record_time

        # The update time read back after our change may include a person's edit
        # made meanwhile: never trust it beyond the latest time GitHub itself
        # attributes to our own action (or, with no such evidence, our record).
        if own_floor is not None:
            if own_bot_last is not None:
                own_floor = min(own_floor, own_bot_last + _SETTLE_TOLERANCE)
            elif records:
                last_record = max(
                    _parse_timestamp(r.created_at.isoformat()) for r in records
                )
                own_floor = min(own_floor, last_record + _BOT_ACTION_TOLERANCE)

        # A lifecycle comment or record is the strongest evidence because it
        # carries the pre-action activity baseline. Without either, use the
        # attributable bot timeline event.
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
        boundaries: list[datetime] = []
        if own_floor is not None:
            boundaries.append(own_floor)  # exact: no guessing for our own actions
        if own_bot_last is not None and (own_floor is None or own_bot_last > own_floor):
            boundaries.append(own_bot_last + _BOT_ACTION_TOLERANCE)
        if other_bot_last is not None:
            boundaries.append(other_bot_last + _BOT_ACTION_TOLERANCE)
        if boundaries and updated > max(boundaries):
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
                    ctx, issue, assignees, days_inactive, last_activity, now
                )
                counts["unassigned"] += len(assignees)
            except _AlreadyClaimed:
                pass
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
            try:
                await self._mark_stale(
                    ctx, issue, cfg.stale_label, days_inactive, last_activity, now
                )
            except _AlreadyClaimed:
                return
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

    async def _post_lifecycle_comment(
        self, owner: str, repo: str, number: int, body: str, inst: int
    ) -> None:
        """Post a lifecycle comment, trying once more after a transient failure.

        The GitHub client does not retry POSTs, and these comments carry the
        marker that preserves an issue's real activity, so one retry is worth it.
        A timeout does not prove GitHub rejected the first attempt, so before
        retrying we look for the same comment, written by this app, on the issue.
        """
        try:
            await self._gh.post_comment(owner, repo, number, body, inst)
        except Exception as exc:
            log.warning(
                "Posting a lifecycle comment on %s/%s#%s failed, retrying once: %s",
                owner, repo, number, exc,
            )
            try:
                existing = await self._gh.list_issue_comments(owner, repo, number, inst)
            except Exception:
                raise exc  # cannot tell whether it landed: do not risk a duplicate
            if any(
                _is_own_comment(comment)
                and (comment.get("body") or "").strip() == body.strip()
                for comment in existing
            ):
                return
            await self._gh.post_comment(owner, repo, number, body, inst)

    async def _mark_stale(
        self, ctx: dict, issue: dict, stale_label: str, days_inactive: int = 0,
        last_activity: datetime | None = None, now: datetime | None = None,
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]

        # Intent first, then the GitHub change, then confirmation (with its log
        # and audit rows, in one transaction), then the notice. A crash or an
        # ambiguous failure at any point is reconciled by the next scan.
        record = await self._record_action(
            ctx, number, "stale", stale_label, last_activity, now
        )
        await self._gh.add_label(owner, repo, number, stale_label, inst)
        await self._confirm_action(ctx, record, days_inactive, None)
        await self._deliver_notice(ctx, record)

    async def _record_action(
        self, ctx: dict, number: int, action: str, detail: str,
        last_activity: datetime | None, now: datetime | None = None,
    ) -> LifecycleAction:
        """Store a bot action durably, before its notice is attempted."""
        record = LifecycleAction(
            owner=ctx["owner"],
            repo=ctx["repo"],
            issue_number=number,
            action=action,
            detail=detail,
            last_activity=last_activity,
            created_at=now or datetime.now(UTC),
        )
        ctx["db"].add(record)
        try:
            await ctx["db"].commit()
        except IntegrityError as exc:
            await ctx["db"].rollback()
            raise _AlreadyClaimed from exc
        return record

    async def _confirm_action(
        self,
        ctx: dict,
        record: LifecycleAction,
        days_inactive: int,
        logins: list[str] | None,
    ) -> None:
        """Mark an intent as applied, writing its log and audit rows with it."""
        owner, repo = ctx["owner"], ctx["repo"]
        number = record.issue_number
        record.applied_at = datetime.now(UTC)
        if record.action == "stale":
            await self._log_stale(ctx, number, "marked_stale", days_inactive)
            await audit.record(
                ctx["db"], action="issue.stale_marked", owner=owner, repo=repo,
                target_number=number, reason="No activity within stale period",
            )
        else:
            await self._log_stale(ctx, number, "unassigned", days_inactive)
            for login in logins or []:
                await audit.record(
                    ctx["db"], action="issue.unassigned", owner=owner, repo=repo,
                    target_number=number, target_login=login,
                    reason="Auto-unassigned due to inactivity",
                )
        await ctx["db"].commit()

    async def _recover_pending_actions(self, ctx: dict) -> None:
        """Settle intents left by a crash or an ambiguous GitHub failure.

        GitHub's actual state decides: a change that landed is confirmed (with
        its bookkeeping), one that did not is forgotten so the normal scan can
        decide again from fresh activity.
        """
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        cfg = ctx["config"].workflows.issue_management
        result = await db.execute(
            select(LifecycleAction.id).where(
                LifecycleAction.owner == owner,
                LifecycleAction.repo == repo,
                LifecycleAction.applied_at.is_(None),
            )
        )
        for record_id in list(result.scalars().all()):
            record = await db.get(LifecycleAction, record_id)
            if record is None:
                continue
            action, number = record.action, record.issue_number
            try:
                issue = await self._gh.get_issue(owner, repo, number, inst)
                days = 0
                if record.last_activity is not None:
                    days = (
                        _parse_timestamp(record.created_at.isoformat())
                        - _parse_timestamp(record.last_activity.isoformat())
                    ).days
                if record.action == "stale":
                    labels = {
                        (lb["name"] if isinstance(lb, dict) else lb).casefold()
                        for lb in issue.get("labels", [])
                    }
                    if cfg.stale_label.casefold() in labels:
                        await self._confirm_action(ctx, record, days, None)
                    else:
                        await db.delete(record)
                        await db.commit()
                else:
                    targeted = [t.lstrip("@") for t in record.detail.split()]
                    current = {
                        person["login"].casefold()
                        for person in (issue.get("assignees") or [])
                        if person and person.get("login")
                    }
                    removed = [t for t in targeted if t.casefold() not in current]
                    if removed:
                        record.detail = " ".join(f"@{t}" for t in removed)
                        await self._confirm_action(ctx, record, days, removed)
                    else:
                        await db.delete(record)
                        await db.commit()
            except Exception as exc:
                await db.rollback()
                log.warning(
                    "Could not settle %s intent #%s on %s/%s#%s: %s",
                    action, record_id, owner, repo, number, exc,
                )

    @staticmethod
    def _notice_body(record: LifecycleAction) -> str:
        if record.action == "unassigned":
            text = (
                f"⏰ {record.detail} — auto-unassigned due to inactivity. "
                f"Re-assign yourself with `/assign` if you're still working on this!"
            )
        else:
            text = (
                f"⚠️ This issue has been marked **stale** due to inactivity.\n\n"
                f"If it's still relevant please comment to keep it open. "
                f"Remove the `{record.detail}` label to exempt it permanently."
            )
        return (
            text
            + f"\n\n{_notice_marker(record.id)}"
            + _marker(record.action, record.last_activity)
        )

    async def _deliver_notice(self, ctx: dict, record: LifecycleAction) -> None:
        """Post a recorded action's notice once; never raise, never duplicate.

        A failure leaves the record pending for the next scan. Before any retry
        the issue is searched for the notice's marker, because a timed-out POST
        may have been accepted.
        """
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        number = record.issue_number

        record.notice_attempts += 1
        try:
            if record.notice_attempts > 1:
                existing = await self._gh.list_issue_comments(owner, repo, number, inst)
                marker = _notice_marker(record.id)
                if any(
                    marker in (comment.get("body") or "") and _is_own_comment(comment)
                    for comment in existing
                ):
                    record.notice_delivered_at = datetime.now(UTC)
            if record.notice_delivered_at is None:
                await self._post_lifecycle_comment(
                    owner, repo, number, self._notice_body(record), inst
                )
                record.notice_delivered_at = datetime.now(UTC)
        except Exception as exc:
            log.warning(
                "Notice for %s on %s/%s#%s not delivered (attempt %s): %s",
                record.action, owner, repo, number, record.notice_attempts, exc,
            )

        # Remember the update time GitHub stamped for our own changes, so a later
        # edit can be told apart from them exactly.
        try:
            current = await self._gh.get_issue(owner, repo, number, inst)
            record.settled_updated_at = _parse_timestamp(current["updated_at"])
        except Exception as exc:
            log.warning(
                "Could not read the update time of %s/%s#%s: %s",
                owner, repo, number, exc,
            )
        await db.commit()

    async def _recover_pending_notices(self, ctx: dict) -> None:
        """Retry notices that failed on an earlier scan."""
        db: AsyncSession = ctx["db"]
        result = await db.execute(
            select(LifecycleAction.id).where(
                LifecycleAction.owner == ctx["owner"],
                LifecycleAction.repo == ctx["repo"],
                LifecycleAction.applied_at.is_not(None),
                LifecycleAction.notice_delivered_at.is_(None),
                LifecycleAction.notice_attempts < _MAX_NOTICE_ATTEMPTS,
            )
        )
        for record_id in list(result.scalars().all()):
            record = await db.get(LifecycleAction, record_id)
            if record is not None:
                await self._deliver_notice(ctx, record)

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
        last_activity: datetime | None = None, now: datetime | None = None,
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]
        mentions = " ".join(f"@{a}" for a in assignees)

        # Intent (naming exactly who is targeted) before the removal; see
        # _mark_stale for the sequence.
        record = await self._record_action(
            ctx, number, "unassigned", mentions, last_activity, now
        )
        await self._gh.remove_assignees(owner, repo, number, assignees, inst)
        await self._confirm_action(ctx, record, days_inactive, assignees)
        await self._deliver_notice(ctx, record)

    async def _log_stale(self, ctx: dict, issue_number: int, action: str, days_inactive: int = 0) -> None:
        record = StaleActionLog(
            owner=ctx["owner"],
            repo=ctx["repo"],
            issue_number=issue_number,
            action=action,
            days_inactive=days_inactive,
        )
        ctx["db"].add(record)