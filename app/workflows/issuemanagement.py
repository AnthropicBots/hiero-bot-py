# app/workflows/issuemanagement.py

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.schema import IssueManagementConfig
from app.db.models import LifecycleAction, PendingIssueCloseNotice, StaleActionLog
from app.github.client import MAX_PAGES, GitHubClient
from app.utils import audit
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("workflow.issuemanagement")

# Lifecycle comments end with a hidden marker recording the issue's last genuine
# activity before the bot acted. GitHub bumps ``updated_at`` for the bot's own
# actions, so without it the inactivity countdown would restart after each one.
# The marker must be the very end of the comment: user text echoed into a comment
# (an issue title) can never end it.
_MARKER_RE = re.compile(
    r"<!--\s*hiero-bot:lifecycle:(?P<action>stale|unassigned|closed)\s+"
    r"last_activity=(?P<last_activity>\S+?)\s*-->\s*\Z"
)

# Comments from before markers existed; honoured only when written by a bot.
_LEGACY_PHRASES = {
    "stale": "marked **stale** due to inactivity",
    "unassigned": "auto-unassigned due to inactivity",
    "closed": "automatically closed** due to continued inactivity",
}

# An ``updated_at`` this close to a bot comment is the bot's own; later is not.
_BOT_ACTION_TOLERANCE = timedelta(seconds=60)

# A notice that keeps failing is given up on after this many delivery attempts.
_MAX_NOTICE_ATTEMPTS = 5

# The update time read back after our change is trusted only this far past the
# last time GitHub attributes to our own action; an edit inside the window cannot
# be told apart from it.
_SETTLE_TOLERANCE = timedelta(seconds=5)

# How long a worker owns a row; must comfortably exceed one action's duration.
_LEASE = timedelta(minutes=30)

# The client stops paginating at MAX_PAGES pages of 100; a history that long may
# be cut short and must not be treated as complete.
_HISTORY_LIMIT = MAX_PAGES * 100


# An unattributed change is given this long for GitHub's timeline to show whose
# it was before the intent is abandoned.
_ATTRIBUTION_GRACE = timedelta(hours=1)


class _AlreadyClaimed(Exception):
    """Another worker (or an earlier unfinished attempt) owns this action."""


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _check_history(items: list, number: int) -> None:
    """Refuse to decide from a history that may have been cut short."""
    if len(items) >= _HISTORY_LIMIT:
        raise RuntimeError(f"History of #{number} may be truncated")


def _floor_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


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


def _is_own_event(event: dict) -> bool:
    """True only when a timeline event was made by this GitHub App."""
    configured_app_id = str(settings.github_app_id or "").strip()
    app = event.get("performed_via_github_app")
    return (
        bool(configured_app_id)
        and _is_bot(event.get("actor"))
        and isinstance(app, dict)
        and app.get("id") is not None
        and str(app["id"]) == configured_app_id
    )


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
        # Leases are measured on the scan's clock, advanced by real elapsed time so
        # a long scan does not hand out leases that are already stale.
        ctx["_lifecycle_clock"] = (now, time.monotonic())
        ctx["_lifecycle_worker"] = uuid.uuid4().hex

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
        if len(comments) >= _HISTORY_LIMIT or len(timeline_events) >= _HISTORY_LIMIT:
            raise RuntimeError(
                f"History of #{issue['number']} may be truncated; "
                "not acting on an incomplete record"
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
        record_stale_at: datetime | None = None
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
                        and label_name.casefold() == ctx["config"].stale_label.casefold()
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
                record_stale_at is None or record_time > record_stale_at
            ):
                record_stale_at = record_time

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

        # When the issue was marked stale: the stale-label event is the authority;
        # our own record of the action is next best, and the notice's timestamp
        # (which can lag the label by however long delivery took) the last resort.
        stale_marked_at = bot_stale_marked_at or record_stale_at or stale_marked_at

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

        # Old notices state the phrase in their first line. Later lines can carry
        # user text (the escalation comment quotes an issue title), so are ignored.
        lines = body.strip().casefold().splitlines()
        first = lines[0] if lines else ""
        for action, phrase in _LEGACY_PHRASES.items():
            if phrase in first:
                return action, None
        return None, None

    async def _decide(
        self,
        ctx: dict,
        cfg: IssueManagementConfig,
        issue: dict,
        now: datetime,
        cutoffs: tuple[datetime, datetime],
    ) -> dict | None:
        """Work out what should be done to one issue, from the issue as given."""
        stale_cutoff, unassign_cutoff = cutoffs

        if issue.get("pull_request"):
            return None  # Skip PRs
        if (issue.get("state") or "open").casefold() != "open":
            return None

        # GitHub label names are case-insensitive, so compare them casefolded.
        labels = {
            (lb["name"] if isinstance(lb, dict) else lb).casefold()
            for lb in issue.get("labels", [])
        }
        if labels & {name.casefold() for name in cfg.exempt_labels}:
            return None

        updated = _parse_timestamp(issue["updated_at"])
        is_stale = ctx["config"].stale_label.casefold() in labels

        # Decide from the activity before this scan acts, so one action's comment
        # cannot influence the next decision.
        last_activity, stale_marked_at = await self._get_last_activity(
            ctx, issue, updated
        )
        assignees = [
            person["login"]
            for person in (issue.get("assignees") or [])
            if person and person.get("login")
        ]
        close_due = False
        if is_stale:
            reference = max(stale_marked_at or last_activity, last_activity)
            close_due = now >= reference + timedelta(days=cfg.close_stale_after_days)
        return {
            "issue": issue,
            "last_activity": last_activity,
            "days_inactive": (now - last_activity).days,
            "assignees": assignees,
            "unassign": bool(assignees) and last_activity < unassign_cutoff,
            "close": is_stale and close_due,
            "stale": not is_stale and last_activity < stale_cutoff,
        }

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
        decision = await self._decide(ctx, cfg, issue, now, cutoffs)
        if not decision or not (
            decision["unassign"] or decision["close"] or decision["stale"]
        ):
            return

        # The listing may be stale: re-read the issue and decide again.
        fresh = await self._gh.get_issue(
            ctx["owner"], ctx["repo"], issue["number"], ctx["installation_id"]
        )
        decision = await self._decide(ctx, cfg, fresh, now, cutoffs)
        if not decision:
            return
        issue = decision["issue"]
        last_activity = decision["last_activity"]
        days_inactive = decision["days_inactive"]

        async def recheck(kind: str, targets: list[str] | None = None) -> bool:
            """Decide again, in full, from the issue as it is right now."""
            current = await self._gh.get_issue(
                ctx["owner"], ctx["repo"], issue["number"], ctx["installation_id"]
            )
            again = await self._decide(ctx, cfg, current, now, cutoffs)
            if not again or not again[kind]:
                return False
            if targets is None:
                return True
            return {t.casefold() for t in targets} <= {
                a.casefold() for a in again["assignees"]
            }

        # Independent of the stale decision below: a failure here must not stop it.
        if decision["unassign"]:
            try:
                confirmed = await self._unassign_inactive(
                    ctx, issue, decision["assignees"], days_inactive,
                    last_activity, now, recheck,
                )
                if confirmed:
                    counts["unassigned"] += len(decision["assignees"])
            except _AlreadyClaimed:
                pass
            except Exception as exc:
                counts["errors"] += 1
                log.warning(
                    "Stale scan %s/%s: could not unassign on #%s: %s",
                    ctx["owner"], ctx["repo"], issue["number"], exc,
                )

        # The close period runs from the stale marking, pushed back by activity.
        if decision["close"]:
            newly_closed = await self._close_stale(
                ctx, issue, days_inactive, last_activity, recheck
            )
            if newly_closed:
                counts["closed"] += 1
            return

        if decision["stale"]:
            try:
                confirmed = await self._mark_stale(
                    ctx, issue, ctx["config"].stale_label, days_inactive, last_activity, now,
                    recheck,
                )
            except _AlreadyClaimed:
                return
            if confirmed:
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
        recheck=None,
    ) -> bool:
        """Returns True only when this App's own change was confirmed."""
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]

        # Intent, GitHub change, confirmation (with log and audit rows), notice.
        # The next scan reconciles a crash at any point.
        record = await self._record_action(
            ctx, number, "stale", stale_label, last_activity, now
        )
        if not await self._still_due(ctx, issue, "stale", recheck):
            await self._withdraw(ctx, record)
            raise _AlreadyClaimed
        if await self._gh.add_label(owner, repo, number, stale_label, inst) is False:
            await self._withdraw(ctx, record)
            return False
        return await self._finish_action(ctx, record, days_inactive, False)

    # ── Ownership of rows ────────────────────────────────────

    @staticmethod
    def _worker(ctx: dict) -> str:
        return ctx.get("_lifecycle_worker") or "direct"

    @staticmethod
    def _now(ctx: dict) -> datetime:
        clock = ctx.get("_lifecycle_clock")
        if not clock:
            return datetime.now(UTC)
        base, started = clock
        return base + timedelta(seconds=time.monotonic() - started)

    async def _claim(self, ctx: dict, model, record_id: int) -> bool:
        """Atomically take a row for this worker; False if another one holds it."""
        db: AsyncSession = ctx["db"]
        now = self._now(ctx)
        worker = self._worker(ctx)
        result = await db.execute(
            update(model)
            .where(
                model.id == record_id,
                or_(
                    model.claimed_by.is_(None),
                    model.claimed_by == worker,
                    model.lease_expires_at <= now,
                ),
            )
            .values(claimed_by=worker, lease_expires_at=now + _LEASE)
        )
        await db.commit()
        return result.rowcount == 1

    async def _release(self, ctx: dict, model, record_id: int) -> None:
        db: AsyncSession = ctx["db"]
        try:
            await db.execute(
                update(model)
                .where(model.id == record_id, model.claimed_by == self._worker(ctx))
                .values(claimed_by=None, lease_expires_at=None)
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.warning("Could not release %s #%s: %s", model.__name__, record_id, exc)

    async def _record_action(
        self, ctx: dict, number: int, action: str, detail: str,
        last_activity: datetime | None, now: datetime | None = None,
    ) -> LifecycleAction:
        """Store (and claim) a bot action durably, before GitHub is changed."""
        record = LifecycleAction(
            owner=ctx["owner"],
            repo=ctx["repo"],
            issue_number=number,
            action=action,
            detail=detail,
            last_activity=last_activity,
            created_at=now or datetime.now(UTC),
            status="pending",
            claimed_by=self._worker(ctx),
            lease_expires_at=self._now(ctx) + _LEASE,
        )
        db: AsyncSession = ctx["db"]
        db.add(record)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise _AlreadyClaimed from exc

        # Another worker may have finished the same action since we decided.
        if last_activity is not None:
            result = await db.execute(
                select(LifecycleAction.detail).where(
                    LifecycleAction.owner == ctx["owner"],
                    LifecycleAction.repo == ctx["repo"],
                    LifecycleAction.issue_number == number,
                    LifecycleAction.action == action,
                    LifecycleAction.applied_at.is_not(None),
                    LifecycleAction.created_at > last_activity,
                    LifecycleAction.id != record.id,
                )
            )
            ours = {t.casefold() for t in detail.split()}
            already = any(
                action == "stale" or ours & {t.casefold() for t in other.split()}
                for other in result.scalars().all()
            )
            if already:
                record_id = record.id
                await db.execute(
                    update(LifecycleAction)
                    .where(LifecycleAction.id == record_id)
                    .values(status="abandoned", claimed_by=None, lease_expires_at=None)
                )
                await db.commit()
                raise _AlreadyClaimed
        return record

    async def _still_due(
        self, ctx: dict, issue: dict, kind: str, recheck, targets=None
    ) -> bool:
        """The last check before a change: the full decision, made again."""
        if recheck is not None:
            return await recheck(kind, targets) if targets else await recheck(kind)
        return True

    async def _withdraw(self, ctx: dict, record: LifecycleAction) -> None:
        """Drop an intent whose change was never made."""
        db: AsyncSession = ctx["db"]
        await db.delete(record)
        await db.commit()

    async def _finish_action(
        self, ctx: dict, record: LifecycleAction, days_inactive: int, unassign: bool
    ) -> bool:
        """Confirm a change only once this App's own event shows it was made.

        A successful API reply proves nothing: adding a label that a person added
        a moment ago succeeds too, and the timeline may lag. Without our event the
        intent stays pending for the next scan to reconcile from history.
        """
        cfg = ctx["config"].workflows.issue_management
        try:
            done, _ = await self._attributed(ctx, cfg, record)
        except Exception:
            await self._release(ctx, LifecycleAction, record.id)
            raise
        if not done:
            # No event of ours (yet): not proof that we made the change. The
            # intent stays pending and the next scan reconciles it from history.
            await self._release(ctx, LifecycleAction, record.id)
            return False
        if unassign:
            confirmed = await self._confirm_action(
                ctx, record, days_inactive, done,
                detail=" ".join(f"@{t}" for t in done),
            )
        else:
            confirmed = await self._confirm_action(ctx, record, days_inactive, None)
        if confirmed:
            await self._deliver_notice(ctx, record)
        return confirmed

    async def _confirm_action(
        self,
        ctx: dict,
        record: LifecycleAction,
        days_inactive: int,
        logins: list[str] | None,
        detail: str | None = None,
    ) -> bool:
        """Mark a pending intent as applied, with its log and audit rows.

        The status change is conditional on this worker still owning the intent,
        so exactly one worker writes the bookkeeping. Returns False when it does
        not.
        """
        db: AsyncSession = ctx["db"]
        owner, repo = ctx["owner"], ctx["repo"]
        number, action = record.issue_number, record.action
        values = {"status": "applied", "applied_at": datetime.now(UTC)}
        if detail is not None:
            values["detail"] = detail
        result = await db.execute(
            update(LifecycleAction)
            .where(
                LifecycleAction.id == record.id,
                LifecycleAction.status == "pending",
                LifecycleAction.claimed_by == self._worker(ctx),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            await db.rollback()
            return False
        if action == "stale":
            await self._log_stale(ctx, number, "marked_stale", days_inactive)
            await audit.record(
                db, action="issue.stale_marked", owner=owner, repo=repo,
                target_number=number, reason="No activity within stale period",
            )
        else:
            await self._log_stale(ctx, number, "unassigned", days_inactive)
            for login in logins or []:
                await audit.record(
                    db, action="issue.unassigned", owner=owner, repo=repo,
                    target_number=number, target_login=login,
                    reason="Auto-unassigned due to inactivity",
                )
        await db.commit()
        await db.refresh(record)
        return True

    async def _recover_pending_actions(self, ctx: dict) -> None:
        """Settle intents left by a crash or an ambiguous GitHub failure.

        Only an intent whose owner is gone (released, or its lease expired) is
        touched, and only after taking it atomically. What happened is read from
        GitHub's *history* -- events made by this App after the intent was
        written -- not from the issue's current state, which a person may have
        changed since. A change that cannot be attributed to us is never
        recorded as ours.
        """
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        cfg = ctx["config"].workflows.issue_management
        result = await db.execute(
            select(LifecycleAction.id).where(
                LifecycleAction.owner == owner,
                LifecycleAction.repo == repo,
                LifecycleAction.status == "pending",
            )
        )
        for record_id in list(result.scalars().all()):
            if not await self._claim(ctx, LifecycleAction, record_id):
                continue  # another worker is on it
            try:
                await self._settle_intent(ctx, cfg, record_id, owner, repo, inst)
            except Exception as exc:
                await db.rollback()
                log.warning(
                    "Could not settle lifecycle intent #%s on %s/%s: %s",
                    record_id, owner, repo, exc,
                )
            finally:
                await self._release(ctx, LifecycleAction, record_id)

    async def _settle_intent(
        self, ctx: dict, cfg, record_id: int, owner: str, repo: str, inst: int
    ) -> None:
        db: AsyncSession = ctx["db"]
        record = await db.get(LifecycleAction, record_id)
        if record is None:
            return
        await db.refresh(record)
        if record.status != "pending":
            return
        number, action = record.issue_number, record.action
        created = _parse_timestamp(record.created_at.isoformat())
        days = 0
        if record.last_activity is not None:
            days = (created - _parse_timestamp(record.last_activity.isoformat())).days

        issue = await self._gh.get_issue(owner, repo, number, inst)
        done, foreign = await self._attributed(ctx, cfg, record)
        waiting = not foreign and self._now(ctx) - created < _ATTRIBUTION_GRACE

        if action == "stale":
            label = ctx["config"].stale_label.casefold()
            present = label in {
                (lb["name"] if isinstance(lb, dict) else lb).casefold()
                for lb in issue.get("labels", [])
            }
            if done:
                await self._confirm_action(ctx, record, days, None)
            elif present and waiting:
                return  # the timeline may not show whose it was yet
            elif present:
                await self._abandon(ctx, record, "label present but not attributable")
            else:
                await db.delete(record)
                await db.commit()
            return

        if done:
            await self._confirm_action(
                ctx, record, days, done, detail=" ".join(f"@{t}" for t in done)
            )
            return
        targeted = [t.lstrip("@") for t in record.detail.split()]
        assigned = {
            person["login"].casefold()
            for person in (issue.get("assignees") or [])
            if person and person.get("login")
        }
        if any(t.casefold() not in assigned for t in targeted):
            if waiting:
                return
            await self._abandon(ctx, record, "assignee gone but not attributable")
        else:
            await db.delete(record)
            await db.commit()

    async def _attributed(
        self, ctx: dict, cfg, record: LifecycleAction
    ) -> tuple[list[str], bool]:
        """What the timeline shows for an intent since it began.

        Returns what this App's own events did (the stale label, or the logins
        removed) and whether someone else made the same change. GitHub stamps
        events to the second while the intent keeps microseconds, so the intent
        time is floored to the second: an event in the same second still counts.
        """
        timeline = await self._gh.list_issue_timeline(
            ctx["owner"], ctx["repo"], record.issue_number, ctx["installation_id"]
        )
        _check_history(timeline, record.issue_number)
        since = _floor_second(_parse_timestamp(record.created_at.isoformat()))
        recent = [
            e for e in timeline
            if e.get("created_at") and _parse_timestamp(e["created_at"]) >= since
        ]
        if record.action == "stale":
            label = ctx["config"].stale_label.casefold()

            def names(own: bool) -> list[str]:
                return [
                    ctx["config"].stale_label
                    for e in recent
                    if _is_own_event(e) is own
                    and e.get("event") == "labeled"
                    and isinstance(e.get("label"), dict)
                    and str(e["label"].get("name", "")).casefold() == label
                ]
        else:
            targeted = [t.lstrip("@") for t in record.detail.split()]

            def names(own: bool) -> list[str]:
                removed = {
                    str((e.get("assignee") or {}).get("login", "")).casefold()
                    for e in recent
                    if _is_own_event(e) is own and e.get("event") == "unassigned"
                }
                return [t for t in targeted if t.casefold() in removed]

        return names(True), bool(names(False))

    async def _abandon(self, ctx: dict, record: LifecycleAction, why: str) -> None:
        """Keep an intent that cannot be reconciled, visibly, instead of guessing."""
        log.warning(
            "Abandoning lifecycle intent #%s (%s on %s/%s#%s): %s",
            record.id, record.action, record.owner, record.repo,
            record.issue_number, why,
        )
        record.status = "abandoned"
        await ctx["db"].commit()

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
        """Post a confirmed action's notice at most once; never raise.

        The row is taken first, so two workers cannot both post. The issue is
        searched for the notice's marker before *every* post, whatever the
        attempt counter says, because a counter that was never committed cannot
        be trusted to say whether an earlier post landed.
        """
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        number, record_id = record.issue_number, record.id

        if not await self._claim(ctx, LifecycleAction, record_id):
            return
        try:
            await db.refresh(record)
            if record.status != "applied":
                return
            record.notice_attempts += 1
            await db.commit()  # the attempt is counted before it is made

            delivered = False
            try:
                existing = await self._gh.list_issue_comments(owner, repo, number, inst)
                marker = _notice_marker(record_id)
                if not any(
                    marker in (comment.get("body") or "") and _is_own_comment(comment)
                    for comment in existing
                ):
                    await self._post_lifecycle_comment(
                        owner, repo, number, self._notice_body(record), inst
                    )
                delivered = True
            except Exception as exc:
                log.warning(
                    "Notice for %s on %s/%s#%s not delivered (attempt %s): %s",
                    record.action, owner, repo, number, record.notice_attempts, exc,
                )

            if delivered:
                record.status = "delivered"
                record.notice_delivered_at = datetime.now(UTC)
            elif record.notice_attempts >= _MAX_NOTICE_ATTEMPTS:
                record.status = "notice_failed"
                log.error(
                    "Giving up on the %s notice for %s/%s#%s after %s attempts; "
                    "retry_failed_notices() makes it eligible again",
                    record.action, owner, repo, number, record.notice_attempts,
                )

            # Remember the update time GitHub stamped, to tell later edits apart.
            try:
                current = await self._gh.get_issue(owner, repo, number, inst)
                record.settled_updated_at = _parse_timestamp(current["updated_at"])
            except Exception as exc:
                log.warning(
                    "Could not read the update time of %s/%s#%s: %s",
                    owner, repo, number, exc,
                )
            await db.commit()
        finally:
            await self._release(ctx, LifecycleAction, record_id)

    async def _recover_pending_notices(self, ctx: dict) -> None:
        """Retry notices that failed on an earlier scan."""
        db: AsyncSession = ctx["db"]
        result = await db.execute(
            select(LifecycleAction.id).where(
                LifecycleAction.owner == ctx["owner"],
                LifecycleAction.repo == ctx["repo"],
                LifecycleAction.status == "applied",
            )
        )
        for record_id in list(result.scalars().all()):
            record = await db.get(LifecycleAction, record_id)
            if record is not None:
                await self._deliver_notice(ctx, record)

    async def failed_notices(self, ctx: dict) -> list[LifecycleAction]:
        """Notices given up on after too many failed attempts."""
        result = await ctx["db"].execute(
            select(LifecycleAction).where(
                LifecycleAction.owner == ctx["owner"],
                LifecycleAction.repo == ctx["repo"],
                LifecycleAction.status == "notice_failed",
            )
        )
        return list(result.scalars().all())

    async def retry_failed_notices(self, ctx: dict) -> int:
        """Make every given-up notice eligible for delivery again.

        A lease left behind by a worker that died after giving up is cleared,
        but a live lease is left alone so an active delivery is not disturbed.
        """
        db: AsyncSession = ctx["db"]
        now = self._now(ctx)
        result = await db.execute(
            update(LifecycleAction)
            .where(
                LifecycleAction.owner == ctx["owner"],
                LifecycleAction.repo == ctx["repo"],
                LifecycleAction.status == "notice_failed",
                or_(
                    LifecycleAction.lease_expires_at.is_(None),
                    LifecycleAction.lease_expires_at <= now,
                    LifecycleAction.claimed_by == self._worker(ctx),
                ),
            )
            .values(
                status="applied",
                notice_attempts=0,
                claimed_by=None,
                lease_expires_at=None,
            )
        )
        await db.commit()
        return result.rowcount

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
            # Take the record first: two workers must not both close the issue
            # or both post its notice.
            if not await self._claim(ctx, PendingIssueCloseNotice, pending_id):
                continue
            try:
                pending_row = await db.execute(
                    select(PendingIssueCloseNotice).where(
                        PendingIssueCloseNotice.id == pending_id
                    )
                )
                pending = pending_row.scalar_one_or_none()

                if pending is None:
                    continue
                await db.refresh(pending)

                number = pending.issue_number
                pending_created_at = pending.created_at

                issue = await self._gh.get_issue(
                    owner, repo, number, inst
                )
                timeline = await self._gh.list_issue_timeline(
                    owner, repo, number, inst
                )
                _check_history(timeline, number)

                # A human reopening or closing the issue after the pending
                # operation was created takes precedence over automation.
                human_intervened = any(
                    event.get("event") in {"reopened", "closed"}
                    and event.get("created_at")
                    and event.get("actor")
                    and not _is_bot(event["actor"])
                    and _parse_timestamp(event["created_at"])
                    >= _floor_second(pending_created_at)
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

                    if last_activity >= _floor_second(pending_created_at):
                        await db.delete(pending)
                        await db.commit()
                        log.info(
                            "Stale scan %s/%s: cancelled pending close for "
                            "issue #%s after new activity",
                            owner, repo, number,
                        )
                        continue

                    due = await self._close_due(ctx, issue, now)
                    if due:
                        # Last look, right before the destructive call.
                        due = await self._close_due(
                            ctx,
                            await self._gh.get_issue(owner, repo, number, inst),
                            now,
                        )
                    if not due:
                        await db.delete(pending)
                        await db.commit()
                        log.info(
                            "Stale scan %s/%s: cancelled pending close for "
                            "issue #%s, no longer stale",
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
                        >= _floor_second(pending_created_at)
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
            finally:
                await self._release(ctx, PendingIssueCloseNotice, pending_id)

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
                    + f"\n\n{notice_marker}"
                    + _marker("closed", pending.last_activity)
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
        recheck=None,
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
                claimed_by=self._worker(ctx),
                lease_expires_at=self._now(ctx) + _LEASE,
            )
            db.add(pending)

            # This commit must happen before the external GitHub operation.
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return False  # another worker created it first
        elif not await self._claim(ctx, PendingIssueCloseNotice, pending.id):
            return False
        else:
            await db.refresh(pending)

        pending_id = pending.id
        try:
            return await self._close_and_notify(
                ctx, pending, owner, repo, number, inst, recheck
            )
        finally:
            await self._release(ctx, PendingIssueCloseNotice, pending_id)

    async def _close_due(self, ctx: dict, issue: dict, now: datetime) -> bool:
        """The full close decision, from the current configuration."""
        cfg = ctx["config"].workflows.issue_management
        decision = await self._decide(
            ctx, cfg, issue, now,
            (
                now - timedelta(days=cfg.stale_issue_days),
                now - timedelta(days=cfg.auto_unassign_inactive_days),
            ),
        )
        return bool(decision and decision["close"])

    @staticmethod
    def _still_closeable(ctx: dict, issue: dict) -> bool:
        """Open, still carrying the stale label, and not exempt."""
        cfg = ctx["config"].workflows.issue_management
        labels = {
            (lb["name"] if isinstance(lb, dict) else lb).casefold()
            for lb in issue.get("labels", [])
        }
        return (
            (issue.get("state") or "open").casefold() == "open"
            and ctx["config"].stale_label.casefold() in labels
            and not labels & {name.casefold() for name in cfg.exempt_labels}
        )

    async def _close_and_notify(
        self, ctx: dict, pending: PendingIssueCloseNotice,
        owner: str, repo: str, number: int, inst: int, recheck=None,
    ) -> bool:
        db: AsyncSession = ctx["db"]
        newly_closed = pending.closed_at is None

        if newly_closed:
            if recheck is not None:
                closeable = await recheck("close")
            else:
                closeable = self._still_closeable(
                    ctx, await self._gh.get_issue(owner, repo, number, inst)
                )
            if not closeable:
                await db.delete(pending)
                await db.commit()
                return False
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

            # Persist the closure before the notice; a failed notice must not lose it.
            await db.commit()

        await self._deliver_close_notice(ctx, pending)

        return newly_closed

    async def _unassign_inactive(
        self, ctx: dict, issue: dict, assignees: list[str], days_inactive: int = 0,
        last_activity: datetime | None = None, now: datetime | None = None,
        recheck=None,
    ) -> bool:
        """Returns True only when this App's own change was confirmed."""
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        number = issue["number"]
        mentions = " ".join(f"@{a}" for a in assignees)

        # Same sequence as _mark_stale; the intent names exactly who is targeted.
        record = await self._record_action(
            ctx, number, "unassigned", mentions, last_activity, now
        )
        if not await self._still_due(ctx, issue, "unassign", recheck, assignees):
            await self._withdraw(ctx, record)
            raise _AlreadyClaimed
        await self._gh.remove_assignees(owner, repo, number, assignees, inst)
        return await self._finish_action(ctx, record, days_inactive, True)

    async def _log_stale(self, ctx: dict, issue_number: int, action: str, days_inactive: int = 0) -> None:
        record = StaleActionLog(
            owner=ctx["owner"],
            repo=ctx["repo"],
            issue_number=issue_number,
            action=action,
            days_inactive=days_inactive,
        )
        ctx["db"].add(record)
