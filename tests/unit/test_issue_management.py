# tests/unit/test_issue_management.py

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import Base
from app.db.models import (
    AuditLog,
    LifecycleAction,
    PendingIssueCloseNotice,
    StaleActionLog,
)
from app.workflows.issuemanagement import (
    IssueManagementWorkflow,
    _AlreadyClaimed,
    _is_own_comment,
)


@pytest.fixture(autouse=True)
def configured_github_app_id(monkeypatch):
    monkeypatch.setattr(
        "app.workflows.issuemanagement.settings.github_app_id",
        "12345",
    )


@pytest.mark.parametrize(
    ("configured_app_id", "app_metadata"),
    [
        ("12345", None),
        ("12345", {}),
        ("", {"id": 12345}),
    ],
)
def test_lifecycle_comment_requires_complete_matching_app_identity(
    monkeypatch, configured_app_id, app_metadata
):
    monkeypatch.setattr(
        "app.workflows.issuemanagement.settings.github_app_id",
        configured_app_id,
    )

    comment = {
        "body": (
            "Notice\n\n"
            "<!-- hiero-bot:lifecycle:stale "
            "last_activity=2000-01-01T00:00:00+00:00 -->"
        ),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
    }

    if app_metadata is not None:
        comment["performed_via_github_app"] = app_metadata

    assert _is_own_comment(comment) is False


def make_issue(number=1, updated_days_ago=65, labels=None, assignees=None, is_pr=False):
    updated = (datetime.now(UTC) - timedelta(days=updated_days_ago)).isoformat()
    issue = {
        "number": number,
        "title": f"Issue #{number}",
        "updated_at": updated,
        "labels": [{"name": label_name} for label_name in (labels or [])],
        "assignees": [{"login": assignee} for assignee in (assignees or [])],
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://github.com/..."}
    return issue


@pytest.mark.asyncio
async def test_marks_stale_after_cutoff(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[make_issue(updated_days_ago=65)])
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts["stale_marked"] == 1
    mock_gh.add_label.assert_awaited()
    mock_gh.post_comment.assert_awaited()


@pytest.mark.asyncio
async def test_marks_stale_with_github_zulu_timestamp(mock_gh, ctx):
    issue = make_issue()
    issue["updated_at"] = "2024-01-01T00:00:00Z"
    now = datetime(2024, 3, 6, tzinfo=UTC)

    mock_gh.list_issues = AsyncMock(return_value=[issue])

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=now)

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 1
    mock_gh.add_label.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_bot_comment_resets_inactivity_when_configured(
    mock_gh, ctx
):
    _configure(
        ctx,
        auto_unassign_inactive_days=9,
        stale_issue_days=20,
        count_bot_comments_as_activity=True,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)

    # With the opt-in enabled, another bot's recent comment counts as
    # activity and restarts the inactivity countdown.
    issue.comments.append({
        "body": "CI workflow completed successfully.",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "github-actions[bot]", "type": "Bot"},
    })
    issue.touch()

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 0
    mock_gh.add_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_own_app_timeline_event_does_not_reset_inactivity(
    mock_gh, ctx
):
    _configure(
        ctx,
        auto_unassign_inactive_days=9,
        stale_issue_days=20,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)

    issue.timeline_events.append({
        "event": "assigned",
        "created_at": clock["now"].isoformat(),
        "actor": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    issue.touch()

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 1
    mock_gh.add_label.assert_awaited_once()


@pytest.mark.asyncio
async def test_own_app_non_stale_label_does_not_set_stale_timestamp(
    mock_gh, ctx
):
    _configure(
        ctx,
        auto_unassign_inactive_days=9,
        stale_issue_days=20,
        close_stale_after_days=7,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    issue.labels.append("stale")
    _wire(mock_gh, issue)

    # The issue was marked stale 30 days ago, but the App added an
    # unrelated label today. That unrelated event must not postpone
    # the close countdown.
    issue.created = clock["now"] - timedelta(days=60)
    issue.comments.append({
        "body": (
            "Marked stale due to inactivity.\n\n"
            "<!-- hiero-bot:lifecycle:stale "
            "last_activity=2025-11-01T00:00:00+00:00 -->"
        ),
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    issue.timeline_events.append({
        "event": "labeled",
        "label": {"name": "needs-triage"},
        "created_at": clock["now"].isoformat(),
        "actor": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["errors"] == 0
    assert counts["closed"] == 1
    mock_gh.close_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_marks_stale_with_naive_timestamp(mock_gh, ctx):
    issue = make_issue()
    issue["updated_at"] = "2024-01-01T00:00:00"
    now = datetime(2024, 3, 6, tzinfo=UTC)

    mock_gh.list_issues = AsyncMock(return_value=[issue])

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=now)

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 1
    mock_gh.add_label.assert_awaited_once()


@pytest.mark.asyncio
async def test_closes_a_stale_issue_after_the_close_period(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=68, labels=["stale"])
    ])
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts["closed"] == 1
    mock_gh.close_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_exempt_labels(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=90, labels=["pinned"])
    ])
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts["stale_marked"] == 0
    mock_gh.add_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_pull_requests(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=90, is_pr=True)
    ])
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts["stale_marked"] == 0


@pytest.mark.asyncio
async def test_auto_unassigns_inactive(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=20, assignees=["sleepy-dev"])
    ])
    mock_gh.get = AsyncMock(return_value={
        "updated_at": (datetime.now(UTC) - timedelta(days=20)).isoformat()
    })
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts["unassigned"] == 1
    mock_gh.remove_assignees.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.issue_management.enabled = False
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts == {}
    mock_gh.list_issues.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_action_within_stale_period(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[make_issue(updated_days_ago=10)])
    wf = IssueManagementWorkflow(mock_gh)
    counts = await wf.run_stale_scan(ctx)
    assert counts["stale_marked"] == 0
    assert counts["closed"] == 0
    mock_gh.add_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_escalation_notifies_team(mock_gh, ctx):
    from app.config.schema import LabelEscalationRule
    ctx["config"].workflows.issue_management.label_escalation_rules = [
        LabelEscalationRule(label="security", notify_team="sec-team")
    ]
    payload = {
        "issue": {"number": 5, "title": "Critical bug"},
        "label": {"name": "security"},
    }
    wf = IssueManagementWorkflow(mock_gh)
    await wf.handle_label_escalation(ctx, payload)
    mock_gh.post_comment.assert_awaited_once()
    body = mock_gh.post_comment.call_args[0][3]
    assert "sec-team" in body
    assert "security" in body


@pytest.mark.asyncio
async def test_label_escalation_no_matching_rule(mock_gh, ctx):
    payload = {"issue": {"number": 5, "title": "Bug"}, "label": {"name": "bug"}}
    wf = IssueManagementWorkflow(mock_gh)
    await wf.handle_label_escalation(ctx, payload)
    mock_gh.post_comment.assert_not_awaited()


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/x")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


async def _logged_issue_numbers(db) -> list[int]:
    rows = await db.execute(select(StaleActionLog.issue_number))
    return sorted(rows.scalars().all())


@pytest.mark.asyncio
async def test_failing_issue_does_not_abort_the_scan(mock_gh, ctx, db):
    async def add_label(owner, repo, number, label, inst):
        if number == 1:
            raise _http_error(403)

    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(number=1, updated_days_ago=400),
        make_issue(number=2, updated_days_ago=300),
        make_issue(number=3, updated_days_ago=250),
    ])
    mock_gh.add_label = AsyncMock(side_effect=add_label)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["errors"] == 1
    assert counts["stale_marked"] == 2
    assert await _logged_issue_numbers(db) == [2, 3]


@pytest.mark.asyncio
async def test_timeline_failure_skips_issue_without_lifecycle_actions(
    mock_gh, ctx, db
):
    async def list_timeline(owner, repo, number, inst):
        if number == 1:
            raise _http_error(503)
        return []

    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(number=1, updated_days_ago=400),
        make_issue(number=2, updated_days_ago=300),
    ])
    mock_gh.list_issue_timeline = AsyncMock(side_effect=list_timeline)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["errors"] == 1
    assert counts["stale_marked"] == 1
    assert await _logged_issue_numbers(db) == [2]

    # Issue #1 must not receive any lifecycle action after its timeline
    # request fails. Issue #2 must still be processed successfully.
    for action in (
        mock_gh.add_label,
        mock_gh.remove_assignees,
        mock_gh.post_comment,
        mock_gh.close_issue,
    ):
        assert all(call.args[2] != 1 for call in action.await_args_list)


@pytest.mark.asyncio
async def test_earlier_audit_rows_survive_a_later_failure(mock_gh, ctx, db):
    async def add_label(owner, repo, number, label, inst):
        if number == 3:
            raise _http_error(403)

    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(number=1, updated_days_ago=400),
        make_issue(number=2, updated_days_ago=300),
        make_issue(number=3, updated_days_ago=250),
    ])
    mock_gh.add_label = AsyncMock(side_effect=add_label)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["errors"] == 1
    assert await _logged_issue_numbers(db) == [1, 2]


@pytest.mark.asyncio
async def test_malformed_issue_is_counted_and_skipped(mock_gh, ctx, db):
    broken = {"number": 9, "labels": [], "assignees": []}  # no updated_at
    mock_gh.list_issues = AsyncMock(return_value=[
        broken,
        make_issue(number=2, updated_days_ago=300),
    ])

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["errors"] == 1
    assert counts["stale_marked"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Security", "SECURITY", "Pinned"])
async def test_exempt_labels_ignore_case(mock_gh, ctx, label):
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=90, labels=[label])
    ])

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["stale_marked"] == 0
    mock_gh.add_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_stale_label_is_recognised_regardless_of_case(mock_gh, ctx):
    # Created earlier as "Stale"; the config says "stale". It must not be
    # marked stale a second time.
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=65, labels=["Stale"])
    ])

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["stale_marked"] == 0
    mock_gh.add_label.assert_not_awaited()


class _FakeGitHubIssue:
    """One issue behaving like GitHub: the bot's own label, assignee change and
    comment bump ``updated_at``, exactly as they do on the real API."""

    def __init__(self, clock, idle_days: int, assignees=None):
        self.clock = clock
        self.updated = clock["now"] - timedelta(days=idle_days)
        self.created = self.updated
        self.labels: list[str] = []
        self.assignees: list[str] = list(assignees or [])
        self.comments: list[dict] = []
        self.timeline_events: list[dict] = []
        self.state = "open"

    def touch(self):
        self.updated = self.clock["now"]

    def human_comment(self, body="Here is a genuine update."):
        """A contributor comment: genuine activity, and it bumps updated_at."""
        self.comments.append({
            "body": body,
            "created_at": self.clock["now"].isoformat(),
            "user": {"login": "contributor", "type": "User"},
        })
        self.touch()

    def add_timeline_event(
        self,
        event: str,
        actor: dict,
        *,
        performed_via_github_app=None,
        label: str | None = None,
        assignee: str | None = None,
    ):
        self.timeline_events.append({
            "event": event,
            "created_at": self.clock["now"].isoformat(),
            "actor": actor,
            "performed_via_github_app": performed_via_github_app,
            "label": {"name": label} if label is not None else None,
            "assignee": {"login": assignee} if assignee is not None else None,
        })

    def as_dict(self):
        return {
            "number": 1,
            "state": self.state,
            "created_at": self.created.isoformat(),
            "updated_at": self.updated.isoformat(),
            "labels": [{"name": name} for name in self.labels],
            "assignees": [{"login": login} for login in self.assignees],
        }


def _wire(mock_gh, issue: _FakeGitHubIssue):
    async def list_issues(*args, **kwargs):
        requested_state = kwargs.get("state", "open")

        if requested_state == "all" or issue.state == "open":
            return [issue.as_dict()]

        return []

    async def add_label(owner, repo, number, label, inst):
        issue.labels.append(label)
        issue.add_timeline_event(
            "labeled",
            {"login": "hiero-bot[bot]", "type": "Bot"},
            performed_via_github_app={"id": 12345},
            label=label,
        )
        issue.touch()

    async def remove_assignees(owner, repo, number, logins, inst):
        for login in logins:
            if login in issue.assignees:
                issue.add_timeline_event(
                    "unassigned",
                    {"login": "hiero-bot[bot]", "type": "Bot"},
                    performed_via_github_app={"id": 12345},
                    assignee=login,
                )
        issue.assignees = [a for a in issue.assignees if a not in logins]
        issue.touch()

    async def post_comment(owner, repo, number, body, inst):
        issue.comments.append({
            "body": body,
            "created_at": issue.clock["now"].isoformat(),
            "user": {"login": "hiero-bot[bot]", "type": "Bot"},
            "performed_via_github_app": {"id": 12345},
        })
        issue.touch()

    async def list_issue_comments(owner, repo, number, inst):
        return list(issue.comments)

    async def list_issue_timeline(owner, repo, number, inst):
        return list(issue.timeline_events)

    async def get_issue(owner, repo, number, inst):
        if number != issue.as_dict()["number"]:
            raise RuntimeError(f"Issue #{number} not found")
        return issue.as_dict()

    async def close_issue(owner, repo, number, inst):
        issue.state = "closed"

    mock_gh.list_issues = AsyncMock(side_effect=list_issues)
    mock_gh.add_label = AsyncMock(side_effect=add_label)
    mock_gh.remove_assignees = AsyncMock(side_effect=remove_assignees)
    mock_gh.post_comment = AsyncMock(side_effect=post_comment)
    mock_gh.list_issue_comments = AsyncMock(side_effect=list_issue_comments)
    mock_gh.list_issue_timeline = AsyncMock(side_effect=list_issue_timeline)
    mock_gh.close_issue = AsyncMock(side_effect=close_issue)
    mock_gh.get_issue = AsyncMock(side_effect=get_issue)


async def _run_daily(wf, ctx, clock, issue, days, on_day=None):
    """Run the scan once a day; return the day each event first happened."""
    start = clock["now"]
    marked = closed = None
    for day in range(days):
        clock["now"] = start + timedelta(days=day)
        if on_day:
            on_day(day)
        await wf.run_stale_scan(ctx, now=clock["now"])
        if marked is None and "stale" in issue.labels:
            marked = day
        if closed is None and issue.state == "closed":
            closed = day
            break
    return marked, closed


def _configure(ctx, **overrides):
    """Override lifecycle settings, as a repo would in .github/hiero-bot.yml."""
    cfg = ctx["config"].workflows.issue_management
    for name, value in overrides.items():
        setattr(cfg, name, value)
    return cfg


@pytest.mark.asyncio
async def test_issue_closes_close_stale_after_days_after_being_marked(mock_gh, ctx):
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=61)
    _wire(mock_gh, issue)

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=120
    )

    assert marked == 0
    assert closed == 7  # close_stale_after_days, not stale_issue_days + 7


@pytest.mark.asyncio
async def test_human_activity_after_marking_delays_the_close(mock_gh, ctx):
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=61)
    _wire(mock_gh, issue)
    start = clock["now"]

    def human_comments_on_day_3(day):
        if day == 3:
            activity_time = start + timedelta(days=3)
            issue.updated = activity_time
            issue.comments.append({
                "body": "Here is a genuine update.",
                "created_at": activity_time.isoformat(),
                "user": {"login": "contributor"},
            })

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=120,
        on_day=human_comments_on_day_3,
    )

    assert marked == 0
    assert closed == 10  # 7 days after the comment on day 3, not day 7


@pytest.mark.asyncio
async def test_recently_marked_stale_issue_is_not_closed_yet(mock_gh, ctx):
    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(updated_days_ago=3, labels=["stale"])
    ])

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx)

    assert counts["closed"] == 0
    mock_gh.close_issue.assert_not_awaited()


# Configurable lifecycle: every threshold below is a deliberately non-default
# value, so these tests fail if the workflow falls back to hardcoded policy.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stale_days", "close_days"),
    [(20, 5), (45, 3), (8, 12)],  # the last one closes *later* than it goes stale
)
async def test_custom_thresholds_drive_marking_and_closing(
    mock_gh, ctx, stale_days, close_days
):
    _configure(ctx, stale_issue_days=stale_days, close_stale_after_days=close_days)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=stale_days + 1)
    _wire(mock_gh, issue)

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=120
    )

    assert marked == 0
    assert closed == close_days


@pytest.mark.asyncio
async def test_custom_unassign_threshold_is_respected(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=40)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=8, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    assert issue.assignees == ["sleepy-dev"]  # 8 days idle < 9

    clock["now"] += timedelta(days=2)  # now 10 days idle
    await wf.run_stale_scan(ctx, now=clock["now"])
    assert issue.assignees == []


@pytest.mark.asyncio
async def test_disabled_workflow_takes_no_lifecycle_actions(mock_gh, ctx):
    from app.config.schema import LabelEscalationRule

    _configure(
        ctx,
        enabled=False,
        label_escalation_rules=[LabelEscalationRule(label="security", notify_team="sec")],
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=400, assignees=["sleepy-dev"])
    issue.labels.append("stale")
    _wire(mock_gh, issue)
    wf = IssueManagementWorkflow(mock_gh)

    counts = await wf.run_stale_scan(ctx, now=clock["now"])
    await wf.handle_label_escalation(
        ctx, {"issue": {"number": 1, "title": "t"}, "label": {"name": "security"}}
    )

    assert counts == {}
    mock_gh.list_issues.assert_not_awaited()
    mock_gh.remove_assignees.assert_not_awaited()
    mock_gh.add_label.assert_not_awaited()
    mock_gh.post_comment.assert_not_awaited()
    mock_gh.close_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_past_both_thresholds_is_unassigned_and_marked_stale_in_one_scan(
    mock_gh, ctx
):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    assert counts["unassigned"] == 1
    assert counts["stale_marked"] == 1
    assert issue.assignees == []
    assert "stale" in issue.labels
    assert counts["closed"] == 0


@pytest.mark.asyncio
async def test_unassignment_does_not_reset_the_stale_countdown(mock_gh, ctx):
    # 12 days idle: past the unassign threshold (9) but not the stale one (20).
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=60
    )

    assert issue.assignees == []
    # Idle since day -12, so more than 20 days idle from day 9. A countdown that
    # restarted at the unassignment comment would only mark it stale on day 21.
    assert marked == 9


@pytest.mark.asyncio
async def test_stale_marking_does_not_extend_the_close_countdown_across_scans(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20, close_stale_after_days=4)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=60
    )

    assert marked == 0
    assert closed == 4  # counted from the stale marking, not from the unassignment


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["comment", "non_comment"])
async def test_genuine_activity_after_housekeeping_restarts_the_countdown(
    mock_gh, ctx, activity
):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    def contributor_returns_on_day_3(day):
        if day == 3:
            if activity == "comment":
                issue.human_comment()
            else:
                # A human label change is recorded in the issue timeline.
                issue.add_timeline_event(
                    "labeled",
                    {"login": "contributor", "type": "User"},
                )
                issue.touch()

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh),
        ctx,
        clock,
        issue,
        days=60,
        on_day=contributor_returns_on_day_3,
    )

    assert marked == 24  # more than 20 days after the day-3 activity


@pytest.mark.asyncio
async def test_human_comment_quoting_bot_text_still_counts_as_activity(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    def contributor_quotes_the_bot(day):
        if day == 3:
            issue.human_comment("> auto-unassigned due to inactivity\n\nI am back!")

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=60,
        on_day=contributor_quotes_the_bot,
    )

    assert marked == 24


@pytest.mark.asyncio
async def test_other_bot_comment_does_not_reset_inactivity_by_default(
    mock_gh, ctx
):
    _configure(
        ctx,
        auto_unassign_inactive_days=9,
        stale_issue_days=20,
        count_bot_comments_as_activity=False,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)

    # This comment updates the issue's updated_at, but must not count as
    # genuine activity when count_bot_comments_as_activity is false.
    issue.comments.append({
        "body": "CI workflow completed successfully.",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "github-actions[bot]", "type": "Bot"},
    })
    issue.touch()

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 1
    mock_gh.add_label.assert_awaited_once()


@pytest.mark.asyncio
async def test_unassignment_comment_mentions_the_assignees(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=40)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["alice", "bob"])
    _wire(mock_gh, issue)

    await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    body = mock_gh.post_comment.call_args[0][3]
    assert "@alice @bob" in body
    assert "auto-unassigned due to inactivity. Re-assign yourself" in body


@pytest.mark.asyncio
async def test_custom_exempt_labels_leave_the_issue_untouched(mock_gh, ctx):
    _configure(ctx, exempt_labels=["keep-open"], auto_unassign_inactive_days=9,
               stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=400, assignees=["sleepy-dev"])
    issue.labels.append("Keep-Open")
    _wire(mock_gh, issue)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    assert counts["unassigned"] == counts["stale_marked"] == counts["closed"] == 0
    assert issue.assignees == ["sleepy-dev"]
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_naive_comment_timestamps_do_not_break_the_scan(mock_gh, ctx):
    _configure(ctx, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)



    issue.comments.append({
        "body": "old comment",
        "created_at": "2025-12-01T00:00:00",
        "user": {"login": "contributor", "type": "User"},
    })
    _wire(mock_gh, issue)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 1


@pytest.mark.asyncio
async def test_marker_forged_by_a_contributor_is_ignored(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=400, assignees=["squatter"])
    _wire(mock_gh, issue)
    # A contributor pastes the hidden marker with a far-future "last activity" to
    # try to keep the issue from ever going stale.
    issue.human_comment(
        "hi <!-- hiero-bot:lifecycle:unassigned last_activity=2099-01-01T00:00:00+00:00 -->"
    )

    clock["now"] += timedelta(days=365)
    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    assert counts["unassigned"] == 1
    assert counts["stale_marked"] == 1
    assert issue.assignees == []


@pytest.mark.asyncio
async def test_transient_failure_of_the_unassign_notice_keeps_the_original_countdown(
    mock_gh, ctx
):
    # One retry rescues a transient failure, so the marker is written and the
    # countdown is exactly what it would have been without the failure.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def fails_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("GitHub returned 503")
        return await real_post_comment(*args, **kwargs)

    mock_gh.post_comment.side_effect = fails_once

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=40
    )

    assert issue.assignees == []
    assert marked == 9


@pytest.mark.asyncio
async def test_persistent_failure_of_the_unassign_notice_keeps_the_original_countdown(
    mock_gh, ctx
):
    # The action is recorded before its notice, so the issue's real activity is
    # kept even when no notice ever reaches GitHub, and retries stop at the cap.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    mock_gh.post_comment.side_effect = RuntimeError("GitHub returned 503")

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=40
    )

    assert issue.assignees == []
    assert marked == 9
    assert mock_gh.post_comment.await_count < 40  # retries are capped


@pytest.mark.asyncio
async def test_failed_unassign_notice_is_delivered_on_a_later_scan(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def down_for_three_posts(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 3:
            raise RuntimeError("GitHub returned 503")
        return await real_post_comment(*args, **kwargs)

    mock_gh.post_comment.side_effect = down_for_three_posts

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=12
    )

    notices = [c for c in issue.comments if "auto-unassigned" in c["body"]]
    assert len(notices) == 1
    assert marked == 9


@pytest.mark.asyncio
async def test_failed_stale_notice_is_logged_and_delivered_later(mock_gh, ctx, db):
    _configure(ctx, stale_issue_days=20, close_stale_after_days=30, auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def down_for_two_posts(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise RuntimeError("GitHub returned 502")
        return await real_post_comment(*args, **kwargs)

    mock_gh.post_comment.side_effect = down_for_two_posts
    wf = IssueManagementWorkflow(mock_gh)

    counts = await wf.run_stale_scan(ctx, now=clock["now"])

    # The label landed and the action is recorded, although no notice got out.
    assert counts["stale_marked"] == 1
    assert counts["errors"] == 0
    assert "stale" in issue.labels
    assert await _logged_issue_numbers(db) == [1]
    assert [c for c in issue.comments if "marked **stale**" in c["body"]] == []

    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1


@pytest.mark.asyncio
async def test_accepted_notice_is_not_posted_twice_by_a_later_scan(mock_gh, ctx):
    # The POST lands but the client never hears back, and the duplicate check
    # fails too. The next scan must find the notice by its marker.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    real_list_comments = mock_gh.list_issue_comments.side_effect
    state = {"posts": 0, "lists_failed": False}

    async def accepted_then_timeout(*args, **kwargs):
        state["posts"] += 1
        await real_post_comment(*args, **kwargs)
        if state["posts"] == 1:
            raise httpx.ReadTimeout("timed out")

    async def list_fails_once_after_the_post(*args, **kwargs):
        if state["posts"] == 1 and not state["lists_failed"]:
            state["lists_failed"] = True
            raise httpx.ReadTimeout("timed out")
        return await real_list_comments(*args, **kwargs)

    mock_gh.post_comment.side_effect = accepted_then_timeout
    mock_gh.list_issue_comments.side_effect = list_fails_once_after_the_post
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    notices = [c for c in issue.comments if "auto-unassigned" in c["body"]]
    assert len(notices) == 1


@pytest.mark.parametrize(("count_bots", "expected_marked"), [(False, 1), (True, 0)])
@pytest.mark.asyncio
async def test_own_non_lifecycle_comment_follows_the_bot_comment_policy(
    mock_gh, ctx, count_bots, expected_marked
):
    # The app's own escalation comments are bot comments like any other: they do
    # not reset inactivity unless the repository opts in.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    ctx["config"].workflows.issue_management.count_bot_comments_as_activity = count_bots
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)
    issue.comments.append({
        "body": "🔔 @org/core — issue labeled `bug` requires your attention.\n\n> **Crash**",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    issue.touch()

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["stale_marked"] == expected_marked


@pytest.mark.asyncio
async def test_retry_does_not_duplicate_a_comment_github_already_accepted(mock_gh, ctx):
    # The first POST lands but the client times out: the retry must notice it.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def accepted_then_timeout(*args, **kwargs):
        attempts["count"] += 1
        await real_post_comment(*args, **kwargs)
        if attempts["count"] == 1:
            raise httpx.ReadTimeout("timed out")

    mock_gh.post_comment.side_effect = accepted_then_timeout

    await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    notices = [c for c in issue.comments if "auto-unassigned" in c["body"]]
    assert len(notices) == 1
    assert attempts["count"] == 1


@pytest.mark.parametrize("seconds", [1, 10, 30, 59, 90])
@pytest.mark.asyncio
async def test_issue_edit_right_after_a_bot_action_counts_exactly(
    mock_gh, ctx, seconds
):
    # The bot's own update time is recorded, so no edit is mistaken for it, however
    # soon it follows.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    assert issue.assignees == []

    clock["now"] += timedelta(seconds=seconds)
    issue.updated = clock["now"]  # the edit

    marked, _ = await _run_daily(wf, ctx, clock, issue, days=40)

    assert marked == 21  # 20 full days after the edit


@pytest.mark.asyncio
async def test_failing_removal_posts_no_notice_until_it_succeeds(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    mock_gh.remove_assignees.side_effect = RuntimeError("GitHub returned 502")

    await _run_daily(IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=3)

    assert mock_gh.post_comment.await_count == 0


@pytest.mark.parametrize("minutes", [0.5, 5, 20, 45])
@pytest.mark.asyncio
async def test_edit_shortly_after_a_bot_action_still_counts_as_activity(
    mock_gh, ctx, minutes
):
    # A person editing, re-assigning or reopening soon after the bot acted only
    # bumps updated_at. It must not be mistaken for the bot's own bump.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    assert issue.assignees == []

    edited = clock["now"] + timedelta(minutes=minutes)
    clock["now"] = edited
    issue.updated = edited
    issue.add_timeline_event(
        "labeled",
        {"login": "contributor", "type": "User"},
    )

    marked, _ = await _run_daily(wf, ctx, clock, issue, days=40)
    # The countdown restarts from the edit (20 full days later), not from day 12's
    # activity, which would mark it on day 9.
    assert marked == 21


@pytest.mark.asyncio
async def test_failed_unassign_notice_does_not_block_stale_marking(mock_gh, ctx):
    # Past both thresholds: the removal works, its notice fails. Stale marking is
    # an independent decision from the same pre-scan activity, so it still runs
    # in this scan and carries the original baseline into the close countdown.
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5, auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def fails_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("GitHub returned 503")
        return await real_post_comment(*args, **kwargs)

    mock_gh.post_comment.side_effect = fails_once

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=12
    )

    assert issue.assignees == []
    assert marked == 0
    assert closed == 5


@pytest.mark.asyncio
async def test_failed_removal_does_not_block_stale_marking(mock_gh, ctx):
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5, auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    mock_gh.remove_assignees.side_effect = RuntimeError("GitHub returned 502")

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=3
    )

    assert marked == 0
    assert issue.assignees == ["sleepy-dev"]


@pytest.mark.asyncio
async def test_failed_stale_notice_still_closes_on_the_right_day(mock_gh, ctx):
    # The label lands, the notice fails. The issue is stale from that scan on, and
    # the close period runs from it -- not from an older event, so not early.
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5, auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def fails_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("GitHub returned 502")
        return await real_post_comment(*args, **kwargs)

    mock_gh.post_comment.side_effect = fails_once

    _, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=20
    )

    assert "stale" in issue.labels
    # Day 0 labelled the issue; the close period (5 days) runs from there.
    assert closed is not None and closed >= 5


@pytest.mark.asyncio
async def test_failed_close_is_recovered_and_notice_is_delivered(
    mock_gh, ctx
):
    _configure(
        ctx,
        stale_issue_days=20,
        close_stale_after_days=5,
        auto_unassign_inactive_days=9,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    real_close_issue = mock_gh.close_issue.side_effect
    attempts = {"count": 0}

    async def fails_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("GitHub returned 502")
        return await real_close_issue(*args, **kwargs)

    mock_gh.close_issue.side_effect = fails_once

    _, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=3
    )

    assert issue.state == "closed"
    assert closed == 1
    assert mock_gh.close_issue.await_count == 2
    assert mock_gh.post_comment.await_count == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_close_timeout_after_github_accepts_close_is_recovered(
    mock_gh, ctx
):
    _configure(
        ctx,
        stale_issue_days=20,
        close_stale_after_days=5,
        auto_unassign_inactive_days=9,
    )
    clock = {"now": datetime.now(UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    async def close_succeeds_then_times_out(owner, repo, number, inst):
        issue.state = "closed"

        # The pending record has already been committed when GitHub is called.
        # Timestamp the simulated event after that commit.
        clock["now"] = datetime.now(UTC) + timedelta(seconds=1)

        issue.add_timeline_event(
            "closed",
            {"login": "hiero-bot[bot]", "type": "Bot"},
            performed_via_github_app={"id": 12345},
        )
        raise RuntimeError("Client timed out after GitHub accepted the close")

    mock_gh.close_issue.side_effect = close_succeeds_then_times_out
    workflow = IssueManagementWorkflow(mock_gh)

    first_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert first_counts["errors"] == 1
    assert mock_gh.close_issue.await_count == 1
    mock_gh.post_comment.assert_not_awaited()

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert len(result.scalars().all()) == 1

    clock["now"] += timedelta(days=1)
    second_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert second_counts["errors"] == 0
    assert second_counts["closed"] == 1
    assert mock_gh.close_issue.await_count == 1
    assert mock_gh.post_comment.await_count == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert result.scalars().all() == []

    result = await ctx["db"].execute(
        select(StaleActionLog).where(
            StaleActionLog.issue_number == 1,
            StaleActionLog.action == "closed",
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_human_comment_cancels_pending_close_while_issue_is_open(
    mock_gh, ctx
):
    _configure(
        ctx,
        stale_issue_days=20,
        close_stale_after_days=5,
        auto_unassign_inactive_days=9,
    )
    clock = {"now": datetime.now(UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    # The pending record is committed, but GitHub fails to close the issue.
    mock_gh.close_issue.side_effect = RuntimeError(
        "GitHub temporarily unavailable"
    )
    workflow = IssueManagementWorkflow(mock_gh)

    first_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert first_counts["errors"] == 1
    assert issue.state == "open"
    assert mock_gh.close_issue.await_count == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert len(result.scalars().all()) == 1

    # A contributor resumes work before recovery gets another chance to close.
    clock["now"] += timedelta(minutes=1)
    issue.human_comment("I am actively working on this issue.")

    mock_gh.close_issue.side_effect = None

    second_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert second_counts["errors"] == 0
    assert second_counts["closed"] == 0
    assert issue.state == "open"
    assert mock_gh.close_issue.await_count == 1
    assert mock_gh.post_comment.await_count == 0

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_human_reopening_cancels_pending_close_notice(mock_gh, ctx):
    _configure(
        ctx,
        stale_issue_days=20,
        close_stale_after_days=5,
        auto_unassign_inactive_days=9,
    )
    clock = {"now": datetime.now(UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    mock_gh.post_comment.side_effect = RuntimeError(
        "GitHub notification temporarily unavailable"
    )
    workflow = IssueManagementWorkflow(mock_gh)

    first_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert first_counts["errors"] == 1
    assert mock_gh.close_issue.await_count == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    pending = result.scalar_one()
    assert pending.closed_at is not None

    # A contributor reopens the issue after the pending close operation.
    clock["now"] = datetime.now(UTC) + timedelta(seconds=2)
    issue.state = "open"
    issue.add_timeline_event(
        "reopened",
        {"login": "contributor", "type": "User"},
    )

    second_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "open"
    assert second_counts["errors"] == 0
    assert second_counts["closed"] == 0
    assert mock_gh.close_issue.await_count == 1
    assert mock_gh.post_comment.await_count == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_accepted_close_notice_timeout_is_deduplicated_on_recovery(
    mock_gh, ctx
):
    _configure(
        ctx,
        stale_issue_days=20,
        close_stale_after_days=5,
        auto_unassign_inactive_days=9,
    )
    clock = {"now": datetime.now(UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    async def post_comment_succeeds_then_times_out(
        owner, repo, number, body, inst
    ):
        issue.comments.append({
            "body": body,
            "created_at": clock["now"].isoformat(),
            "user": {"login": "hiero-bot[bot]", "type": "Bot"},
            "performed_via_github_app": {"id": 12345},
        })
        issue.touch()
        raise RuntimeError(
            "Client timed out after GitHub accepted the notification"
        )

    mock_gh.post_comment.side_effect = (
        post_comment_succeeds_then_times_out
    )
    workflow = IssueManagementWorkflow(mock_gh)

    first_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert first_counts["errors"] == 1
    assert mock_gh.close_issue.await_count == 1
    assert mock_gh.post_comment.await_count == 1

    # The comment exists remotely, but the outbox record must remain because
    # the client did not receive confirmation.
    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert len(result.scalars().all()) == 1

    clock["now"] += timedelta(days=1)

    second_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert second_counts["errors"] == 0
    assert mock_gh.close_issue.await_count == 1
    assert mock_gh.post_comment.await_count == 1

    close_notices = [
        comment
        for comment in issue.comments
        if "<!-- hiero-bot:close-notice:" in (comment.get("body") or "")
    ]
    assert len(close_notices) == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_failed_close_notice_is_recovered_without_closing_twice(
    mock_gh, ctx
):
    _configure(
        ctx,
        stale_issue_days=20,
        close_stale_after_days=5,
        auto_unassign_inactive_days=9,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    real_post_comment = mock_gh.post_comment.side_effect
    attempts = {"count": 0}

    async def fails_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("GitHub returned 503")
        return await real_post_comment(*args, **kwargs)

    mock_gh.post_comment.side_effect = fails_once
    workflow = IssueManagementWorkflow(mock_gh)

    first_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert first_counts["errors"] == 1
    assert mock_gh.close_issue.await_count == 1

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert len(result.scalars().all()) == 1

    clock["now"] += timedelta(days=1)
    second_counts = await workflow.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "closed"
    assert second_counts["errors"] == 0
    assert mock_gh.close_issue.await_count == 1
    assert mock_gh.post_comment.await_count == 2

    result = await ctx["db"].execute(
        select(PendingIssueCloseNotice)
    )
    assert result.scalars().all() == []

    result = await ctx["db"].execute(
        select(StaleActionLog).where(
            StaleActionLog.issue_number == 1,
            StaleActionLog.action == "closed",
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_issue_body_edit_after_a_bot_action_counts_as_activity(mock_gh, ctx):
    # An edit leaves no comment and no timeline event, only a newer updated_at.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    assert issue.assignees == []

    clock["now"] += timedelta(hours=1)
    issue.updated = clock["now"]  # the edit

    marked, _ = await _run_daily(wf, ctx, clock, issue, days=40)

    assert marked == 21  # 20 full days after the edit, not day 8 from old activity


@pytest.mark.asyncio
async def test_other_bot_comment_does_not_hide_a_later_issue_edit(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=60)
    _wire(mock_gh, issue)
    issue.comments.append({
        "body": "automated note",
        "created_at": (clock["now"] - timedelta(days=50)).isoformat(),
        "user": {"login": "ci[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 999},
    })
    issue.updated = clock["now"] - timedelta(days=2)  # a person edited the body

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["stale_marked"] == 0


@pytest.mark.asyncio
async def test_marker_with_an_unknown_action_is_ignored(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=0, assignees=[])
    _wire(mock_gh, issue)
    issue.comments.append({
        "body": "Note\n\n<!-- hiero-bot:lifecycle:bogus last_activity=2000-01-01T00:00:00+00:00 -->",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    issue.touch()
    wf = IssueManagementWorkflow(mock_gh)

    counts = await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(days=1))

    assert counts["stale_marked"] == 0


@pytest.mark.asyncio
async def test_marker_from_another_app_is_ignored(mock_gh, ctx, monkeypatch):
    monkeypatch.setattr("app.workflows.issuemanagement.settings.github_app_id", "12345")
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=0, assignees=[])
    _wire(mock_gh, issue)
    issue.comments.append({
        "body": "Echo\n\n<!-- hiero-bot:lifecycle:stale last_activity=2000-01-01T00:00:00+00:00 -->",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 999999},
    })
    issue.touch()
    wf = IssueManagementWorkflow(mock_gh)

    counts = await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(days=1))

    assert counts["stale_marked"] == 0


@pytest.mark.asyncio
async def test_marker_smuggled_in_through_an_echoed_issue_title_is_ignored(mock_gh, ctx):
    # The label-escalation comment is posted by the bot and echoes the issue title,
    # which anyone who can open an issue controls.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=0, assignees=["squatter"])
    _wire(mock_gh, issue)
    title = "Crash <!-- hiero-bot:lifecycle:stale last_activity=2099-01-01T00:00:00+00:00 -->"
    issue.comments.append({
        "body": f"🔔 @org/core — issue labeled `bug` requires your attention.\n\n> **{title}**",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    issue.touch()

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=60
    )

    assert issue.assignees == []
    assert marked == 21  # 20 days idle counted from the day-0 comment, not from 2099


@pytest.mark.asyncio
async def test_marker_recording_a_future_activity_is_clamped_to_its_comment(mock_gh, ctx):
    _configure(
        ctx,
        auto_unassign_inactive_days=9,
        stale_issue_days=20,
        count_bot_comments_as_activity=True,
    )
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=400, assignees=["squatter"])
    _wire(mock_gh, issue)
    issue.comments.append({
        "body": "hello\n\n<!-- hiero-bot:lifecycle:unassigned last_activity=2099-01-01T00:00:00+00:00 -->",
        "created_at": clock["now"].isoformat(),
        "user": {"login": "some-other-app[bot]", "type": "Bot"},
    })
    issue.touch()

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=60
    )

    assert issue.assignees == []
    assert marked == 21


@pytest.mark.asyncio
async def test_rate_limited_unassignment_does_not_reset_the_stale_countdown(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    async def removal_after_a_long_retry_wait(owner, repo, number, logins, inst):
        issue.assignees = [a for a in issue.assignees if a not in logins]
        issue.updated = clock["now"] + timedelta(minutes=40)  # the client waited

    mock_gh.remove_assignees.side_effect = removal_after_a_long_retry_wait

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=40
    )

    assert issue.assignees == []
    assert marked == 9


@pytest.mark.asyncio
async def test_failing_removal_is_retried_and_keeps_the_stale_countdown(mock_gh, ctx):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)

    real_remove = mock_gh.remove_assignees.side_effect
    attempts = {"count": 0}

    async def fails_three_times(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 3:
            raise RuntimeError("GitHub returned 502")
        return await real_remove(*args, **kwargs)

    mock_gh.remove_assignees.side_effect = fails_three_times

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=40
    )

    assert issue.assignees == []
    assert marked == 9


# Crash safety: the intent is stored before GitHub is called, so a process that
# dies at any point leaves enough behind for the next scan to finish the job.


class _Crash(BaseException):
    """Simulates the process dying: not caught by the scan's error handling."""


async def _count(db, model) -> int:
    return len((await db.execute(select(model))).scalars().all())


def _crash_setup(mock_gh, ctx, idle_days=25, assignees=None):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20,
               close_stale_after_days=30)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=idle_days, assignees=assignees)
    _wire(mock_gh, issue)
    return clock, issue


@pytest.mark.asyncio
async def test_crash_before_the_label_request_loses_nothing(mock_gh, ctx, db):
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.add_label.side_effect
    mock_gh.add_label.side_effect = _Crash()
    wf = IssueManagementWorkflow(mock_gh)

    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    assert issue.labels == []
    assert await _count(db, LifecycleAction) == 1  # the intent survived

    mock_gh.add_label.side_effect = real
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert issue.labels == ["stale"]
    assert await _logged_issue_numbers(db) == [1]
    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1


@pytest.mark.asyncio
async def test_crash_after_the_label_landed_is_reconciled_without_a_second_label(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.add_label.side_effect

    async def lands_then_crashes(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.add_label.side_effect = lands_then_crashes
    wf = IssueManagementWorkflow(mock_gh)

    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    assert issue.labels == ["stale"]
    assert await _logged_issue_numbers(db) == []  # not yet confirmed

    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert mock_gh.add_label.await_count == 1
    assert await _logged_issue_numbers(db) == [1]
    assert await _count(db, AuditLog) == 1
    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1


@pytest.mark.asyncio
async def test_timeout_after_the_label_landed_is_reconciled(mock_gh, ctx, db):
    clock, _ = _crash_setup(mock_gh, ctx)
    real = mock_gh.add_label.side_effect

    async def lands_then_times_out(*args, **kwargs):
        await real(*args, **kwargs)
        raise httpx.ReadTimeout("timed out")

    mock_gh.add_label.side_effect = lands_then_times_out
    wf = IssueManagementWorkflow(mock_gh)

    first = await wf.run_stale_scan(ctx, now=clock["now"])
    assert first["errors"] == 1

    clock["now"] += timedelta(days=1)
    second = await wf.run_stale_scan(ctx, now=clock["now"])

    assert second["errors"] == 0
    assert mock_gh.add_label.await_count == 1
    assert await _logged_issue_numbers(db) == [1]


@pytest.mark.asyncio
async def test_crash_after_the_removal_landed_keeps_baseline_and_audit(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["sleepy-dev"])
    real = mock_gh.remove_assignees.side_effect

    async def lands_then_crashes(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.remove_assignees.side_effect = lands_then_crashes
    wf = IssueManagementWorkflow(mock_gh)

    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    assert issue.assignees == []

    mock_gh.remove_assignees.side_effect = real
    marked, _ = await _run_daily(wf, ctx, clock, issue, days=40)

    assert mock_gh.remove_assignees.await_count == 1
    assert marked == 9  # the original countdown, not one restarted by the removal
    audits = (await db.execute(select(AuditLog).where(
        AuditLog.action == "issue.unassigned"))).scalars().all()
    assert [a.target_login for a in audits] == ["sleepy-dev"]
    assert len([c for c in issue.comments if "auto-unassigned" in c["body"]]) == 1


@pytest.mark.asyncio
async def test_partial_removal_confirms_only_who_was_removed(mock_gh, ctx, db):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["alice", "bob"])

    async def removes_alice_then_crashes(owner, repo, number, logins, inst):
        issue.assignees = [a for a in issue.assignees if a != "alice"]
        issue.add_timeline_event(
            "unassigned",
            {"login": "hiero-bot[bot]", "type": "Bot"},
            performed_via_github_app={"id": 12345},
            assignee="alice",
        )
        issue.touch()
        raise _Crash()

    real_remove = mock_gh.remove_assignees.side_effect
    mock_gh.remove_assignees.side_effect = removes_alice_then_crashes
    wf = IssueManagementWorkflow(mock_gh)

    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    mock_gh.remove_assignees.side_effect = real_remove
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    notice = [c for c in issue.comments if "auto-unassigned" in c["body"]]
    assert len(notice) >= 1
    assert "@alice" in notice[0]["body"] and "@bob" not in notice[0]["body"]
    audits = (await db.execute(select(AuditLog).where(
        AuditLog.action == "issue.unassigned"))).scalars().all()
    assert sorted(a.target_login for a in audits) == ["alice", "bob"]
    assert issue.assignees == []


@pytest.mark.asyncio
async def test_someone_assigned_after_a_failed_removal_is_not_removed_by_recovery(
    mock_gh, ctx
):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["sleepy-dev"])
    mock_gh.remove_assignees.side_effect = _Crash()
    wf = IssueManagementWorkflow(mock_gh)

    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    real = _wire  # reinstall the working fake, then a person adds an assignee
    real(mock_gh, issue)
    issue.assignees.append("newcomer")
    issue.add_timeline_event("assigned", {"login": "maintainer", "type": "User"})
    issue.touch()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert "newcomer" in issue.assignees  # fresh activity: nobody is removed


@pytest.mark.asyncio
async def test_two_workers_cannot_both_claim_the_same_action(mock_gh, ctx, db):
    wf = IssueManagementWorkflow(mock_gh)
    await wf._record_action(ctx, 7, "stale", "stale", None, None)

    with pytest.raises(_AlreadyClaimed):
        await wf._record_action(ctx, 7, "stale", "stale", None, None)

    await wf._record_action(ctx, 7, "unassigned", "@a", None, None)  # other action
    await wf._record_action(ctx, 8, "stale", "stale", None, None)  # other issue
    assert await _count(db, LifecycleAction) == 3


@pytest.mark.asyncio
async def test_claimed_issue_is_skipped_not_reported_as_an_error(mock_gh, ctx):
    clock, _ = _crash_setup(mock_gh, ctx)
    wf = IssueManagementWorkflow(mock_gh)
    await wf._record_action(ctx, 1, "stale", "stale", None, None)
    # Another worker holds a live lease on it, as if it were mid-action.

    counts = await wf.run_stale_scan(ctx, now=clock["now"])

    assert counts["errors"] == 0
    assert counts["stale_marked"] == 0
    mock_gh.add_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_rows_survive_a_crash_before_the_notice(mock_gh, ctx, db):
    clock, _ = _crash_setup(mock_gh, ctx)
    mock_gh.post_comment.side_effect = _Crash()
    wf = IssueManagementWorkflow(mock_gh)

    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    assert await _logged_issue_numbers(db) == [1]
    assert await _count(db, AuditLog) == 1


@pytest.mark.asyncio
async def test_human_edit_before_the_readback_is_not_swallowed(mock_gh, ctx):
    # A person edits the issue after the bot's notice but before the bot reads the
    # update time back. That read-back must not become the bot's own boundary.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    real_get = mock_gh.get_issue.side_effect
    edited = {"done": False}

    async def edit_lands_first(*args, **kwargs):
        if not edited["done"]:
            edited["done"] = True
            issue.updated = clock["now"] + timedelta(seconds=30)  # the edit
        return await real_get(*args, **kwargs)

    mock_gh.get_issue.side_effect = edit_lands_first
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    clock["now"] += timedelta(seconds=30)
    marked, _ = await _run_daily(wf, ctx, clock, issue, days=40)

    assert marked == 21  # counted from the edit, not from the day-12 activity


# Several workers, one database: ownership is a lease on the row, taken with a
# conditional UPDATE, so these tests use separate sessions on a real file.


@pytest.fixture
async def two_sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workers.db'}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as first, factory() as second, factory() as observer:
        yield first, second, observer
    await engine.dispose()


def _as_worker(ctx, session, name, now):
    worker = {**ctx, "db": session}
    worker["_lifecycle_clock"] = (now, time.monotonic())
    worker["_lifecycle_worker"] = name
    return worker


@pytest.mark.asyncio
async def test_two_workers_scanning_together_produce_one_action(
    mock_gh, ctx, two_sessions
):
    first, second, observer = two_sessions
    clock, issue = _crash_setup(mock_gh, ctx)
    wf = IssueManagementWorkflow(mock_gh)

    await asyncio.gather(
        wf.run_stale_scan({**ctx, "db": first}, now=clock["now"]),
        wf.run_stale_scan({**ctx, "db": second}, now=clock["now"]),
    )

    assert issue.labels == ["stale"]
    assert mock_gh.add_label.await_count == 1
    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1
    assert await _count(observer, AuditLog) == 1
    assert await _logged_issue_numbers(observer) == [1]


@pytest.mark.asyncio
async def test_recovery_leaves_an_intent_alone_while_its_owner_is_mid_request(
    mock_gh, ctx, two_sessions
):
    first, second, observer = two_sessions
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.add_label.side_effect
    started, gate = asyncio.Event(), asyncio.Event()

    async def slow_label(*args, **kwargs):
        started.set()
        await gate.wait()
        return await real(*args, **kwargs)

    mock_gh.add_label.side_effect = slow_label
    wf = IssueManagementWorkflow(mock_gh)

    worker_a = asyncio.create_task(
        wf.run_stale_scan({**ctx, "db": first}, now=clock["now"])
    )
    await started.wait()

    # Worker B starts its scan while A's label request has not taken effect yet.
    counts_b = await wf.run_stale_scan({**ctx, "db": second}, now=clock["now"])

    pending = (await observer.execute(select(LifecycleAction))).scalars().all()
    assert [r.status for r in pending] == ["pending"]  # not deleted, not duplicated
    assert counts_b["stale_marked"] == 0 and counts_b["errors"] == 0

    gate.set()
    await worker_a

    assert issue.labels == ["stale"]
    assert mock_gh.add_label.await_count == 1
    assert await _count(observer, AuditLog) == 1
    row = (await observer.execute(select(LifecycleAction))).scalars().one()
    await observer.refresh(row)
    assert row.status == "delivered"


@pytest.mark.asyncio
async def test_two_workers_settling_one_expired_intent_write_one_audit_trail(
    mock_gh, ctx, two_sessions
):
    first, second, observer = two_sessions
    clock, issue = _crash_setup(mock_gh, ctx)
    await _wire_label_landed(issue, clock)
    first.add(LifecycleAction(
        owner="hiero", repo="sdk-js", issue_number=1, action="stale", detail="stale",
        created_at=clock["now"], status="pending", claimed_by="dead-worker",
        lease_expires_at=clock["now"] - timedelta(minutes=1),
    ))
    await first.commit()
    wf = IssueManagementWorkflow(mock_gh)

    await asyncio.gather(
        wf._recover_pending_actions(_as_worker(ctx, first, "a", clock["now"])),
        wf._recover_pending_actions(_as_worker(ctx, second, "b", clock["now"])),
    )

    assert await _count(observer, AuditLog) == 1
    assert await _logged_issue_numbers(observer) == [1]


@pytest.mark.asyncio
async def test_two_workers_delivering_one_notice_post_it_once(
    mock_gh, ctx, two_sessions
):
    first, second, _ = two_sessions
    clock, issue = _crash_setup(mock_gh, ctx)
    first.add(LifecycleAction(
        owner="hiero", repo="sdk-js", issue_number=1, action="stale", detail="stale",
        created_at=clock["now"], status="applied", applied_at=clock["now"],
    ))
    await first.commit()
    wf = IssueManagementWorkflow(mock_gh)

    # Both workers read the comments before either posts, as they would over a
    # real network.
    real_list = mock_gh.list_issue_comments.side_effect

    async def slow_list(*args, **kwargs):
        seen = await real_list(*args, **kwargs)
        await asyncio.sleep(0.05)
        return seen

    mock_gh.list_issue_comments.side_effect = slow_list

    await asyncio.gather(
        wf._recover_pending_notices(_as_worker(ctx, first, "a", clock["now"])),
        wf._recover_pending_notices(_as_worker(ctx, second, "b", clock["now"])),
    )

    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1


async def _wire_label_landed(issue, clock):
    issue.labels.append("stale")
    issue.add_timeline_event(
        "labeled",
        {"login": "hiero-bot[bot]", "type": "Bot"},
        performed_via_github_app={"id": 12345},
        label="stale",
    )


@pytest.mark.asyncio
async def test_a_live_lease_is_respected_and_an_expired_one_is_taken_over(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)
    await _wire_label_landed(issue, clock)
    db.add(LifecycleAction(
        owner="hiero", repo="sdk-js", issue_number=1, action="stale", detail="stale",
        created_at=clock["now"], status="pending", claimed_by="other-worker",
        lease_expires_at=clock["now"] + timedelta(minutes=10),
    ))
    await db.commit()
    wf = IssueManagementWorkflow(mock_gh)

    await wf.run_stale_scan(ctx, now=clock["now"])
    assert await _count(db, AuditLog) == 0  # still the other worker's

    await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(minutes=45))
    assert await _count(db, AuditLog) == 1  # its lease ran out


# Recovery reads history, not the issue's present state.


@pytest.mark.asyncio
async def test_a_stale_label_added_by_a_person_is_not_recorded_as_ours(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)
    mock_gh.add_label.side_effect = _Crash()
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    issue.labels.append("stale")  # a maintainer, while the bot was down
    issue.add_timeline_event("labeled", {"login": "maintainer", "type": "User"},
                             label="stale")
    issue.touch()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert await _count(db, AuditLog) == 0
    assert await _logged_issue_numbers(db) == []
    assert [c for c in issue.comments if "marked **stale**" in c["body"]] == []
    row = (await db.execute(select(LifecycleAction))).scalars().one()
    assert row.status == "abandoned"  # kept and visible, not guessed at


@pytest.mark.asyncio
async def test_an_assignee_removed_by_a_person_is_not_recorded_as_ours(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["alice"])
    mock_gh.remove_assignees.side_effect = _Crash()
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    issue.assignees.remove("alice")
    issue.add_timeline_event("unassigned", {"login": "maintainer", "type": "User"},
                             assignee="alice")
    issue.touch()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert await _count(db, AuditLog) == 0
    assert [c for c in issue.comments if "auto-unassigned" in c["body"]] == []


@pytest.mark.asyncio
async def test_our_action_stays_on_record_after_a_person_reverses_it(mock_gh, ctx, db):
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.add_label.side_effect

    async def lands_then_crashes(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.add_label.side_effect = lands_then_crashes
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    issue.labels.remove("stale")  # a maintainer takes it off again
    issue.add_timeline_event("unlabeled", {"login": "maintainer", "type": "User"},
                             label="stale")
    issue.touch()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert await _count(db, AuditLog) == 1
    assert await _logged_issue_numbers(db) == [1]


@pytest.mark.asyncio
async def test_a_reassigned_user_removed_by_us_stays_on_record(mock_gh, ctx, db):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["alice"])
    real = mock_gh.remove_assignees.side_effect

    async def lands_then_crashes(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.remove_assignees.side_effect = lands_then_crashes
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    issue.assignees.append("alice")  # put back by a maintainer
    issue.add_timeline_event("assigned", {"login": "maintainer", "type": "User"},
                             assignee="alice")
    issue.touch()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    audits = (await db.execute(select(AuditLog).where(
        AuditLog.action == "issue.unassigned"))).scalars().all()
    assert [a.target_login for a in audits] == ["alice"]


@pytest.mark.asyncio
async def test_missing_timeline_never_fabricates_a_bot_action(mock_gh, ctx, db):
    clock, issue = _crash_setup(mock_gh, ctx)
    mock_gh.add_label.side_effect = _Crash()
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    issue.labels.append("stale")  # the label is there, but the timeline is empty
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert await _count(db, AuditLog) == 0
    row = (await db.execute(select(LifecycleAction))).scalars().one()
    assert row.status == "abandoned"


# The issue is looked at again immediately before anything is changed.


def _stale_listing(mock_gh, issue, first_calls=1):
    """The first look at the comments is old; later looks see what is there."""
    real = mock_gh.list_issue_comments.side_effect
    calls = {"n": 0}

    async def comments(*args, **kwargs):
        calls["n"] += 1
        return [] if calls["n"] <= first_calls else await real(*args, **kwargs)

    mock_gh.list_issue_comments.side_effect = comments


@pytest.mark.asyncio
async def test_a_comment_after_the_listing_prevents_marking_stale(mock_gh, ctx):
    clock, issue = _crash_setup(mock_gh, ctx)
    issue.human_comment("Still on it!")
    _stale_listing(mock_gh, issue)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["stale_marked"] == 0
    mock_gh.add_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_assignment_change_after_the_listing_prevents_unassigning(
    mock_gh, ctx
):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["alice"])
    listing = mock_gh.list_issues.side_effect

    async def stale_snapshot(*args, **kwargs):
        snapshot = await listing(*args, **kwargs)
        issue.assignees = []  # a maintainer removed her right after the listing
        return snapshot

    mock_gh.list_issues.side_effect = stale_snapshot

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["unassigned"] == 0
    mock_gh.remove_assignees.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_comment_after_the_listing_prevents_closing(mock_gh, ctx):
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5,
               auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)
    issue.human_comment("I am back.")
    _stale_listing(mock_gh, issue)

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["closed"] == 0
    mock_gh.close_issue.assert_not_awaited()


# Notices: found by marker before every post, and bounded.


@pytest.mark.asyncio
async def test_a_crash_after_the_comment_was_accepted_does_not_post_it_twice(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.post_comment.side_effect

    async def accepted_then_crash(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.post_comment.side_effect = accepted_then_crash
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    mock_gh.post_comment.side_effect = real
    await db.rollback()  # the process died before anything more was written
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1
    row = (await db.execute(select(LifecycleAction))).scalars().one()
    await db.refresh(row)
    assert row.status == "delivered"


@pytest.mark.asyncio
async def test_notice_is_given_up_on_after_exactly_five_attempts_and_can_be_retried(
    mock_gh, ctx, db, hiero_caplog
):
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.post_comment.side_effect
    mock_gh.post_comment.side_effect = RuntimeError("GitHub returned 503")
    wf = IssueManagementWorkflow(mock_gh)

    for _ in range(9):
        await wf.run_stale_scan(ctx, now=clock["now"])
        clock["now"] += timedelta(days=1)

    row = (await db.execute(select(LifecycleAction))).scalars().one()
    await db.refresh(row)
    assert row.notice_attempts == 5  # no sixth attempt
    assert row.status == "notice_failed"
    assert mock_gh.post_comment.await_count == 10  # each attempt retries once
    assert [r.id for r in await wf.failed_notices(ctx)] == [row.id]
    assert "Giving up on the stale notice" in hiero_caplog.text

    mock_gh.post_comment.side_effect = real
    assert await wf.retry_failed_notices(ctx) == 1
    await wf.run_stale_scan(ctx, now=clock["now"])
    await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(days=1))

    await db.refresh(row)
    assert row.status == "delivered"
    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1
    assert await wf.failed_notices(ctx) == []


@pytest.mark.parametrize("delivered_on_day", [3, None])
@pytest.mark.asyncio
async def test_close_period_runs_from_the_label_not_from_the_delayed_notice(
    mock_gh, ctx, delivered_on_day
):
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5,
               auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=25)
    _wire(mock_gh, issue)
    start = clock["now"]
    real = mock_gh.post_comment.side_effect

    async def delayed(*args, **kwargs):
        day = (clock["now"] - start).days
        if delivered_on_day is None or day < delivered_on_day:
            raise RuntimeError("GitHub returned 503")
        return await real(*args, **kwargs)

    mock_gh.post_comment.side_effect = delayed

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=20
    )

    assert marked == 0
    assert closed == 5  # from the label on day 0, not from the notice on day 3


@pytest.mark.asyncio
async def test_close_notice_ends_with_the_lifecycle_marker(mock_gh, ctx):
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5,
               auto_unassign_inactive_days=9, count_bot_comments_as_activity=True)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)

    await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    closing = [c for c in issue.comments if "automatically closed" in c["body"]]
    assert len(closing) == 1
    action, baseline = IssueManagementWorkflow._classify_comment(closing[0])
    assert action == "closed"
    assert baseline is not None


@pytest.mark.asyncio
async def test_history_that_may_be_truncated_is_not_acted_on(mock_gh, ctx):
    clock, _ = _crash_setup(mock_gh, ctx)
    mock_gh.list_issue_comments.side_effect = None
    mock_gh.list_issue_comments.return_value = [
        {"body": "x", "created_at": "2025-01-01T00:00:00+00:00",
         "user": {"login": "someone", "type": "User"}}
    ] * 5000

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["errors"] == 1
    mock_gh.add_label.assert_not_awaited()


# Documented limits: an edit that GitHub stamps at the very moment of the bot's own
# update cannot be told apart from it, because an edit leaves no event.


@pytest.mark.asyncio
async def test_edit_at_the_same_moment_as_the_bot_update_is_not_detectable(
    mock_gh, ctx
):
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    real_post = mock_gh.post_comment.side_effect

    async def edit_lands_with_the_comment(*args, **kwargs):
        await real_post(*args, **kwargs)  # the comment's update time ...
        # ... is the same instant as a person's edit: updated_at cannot show both.

    mock_gh.post_comment.side_effect = edit_lands_with_the_comment

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=40
    )

    assert marked == 9


# A person's change between the last check and the mutation.

_parse_ts = datetime.fromisoformat
_PERSON = {"login": "maintainer", "type": "User"}


def _stale_issue(mock_gh, ctx):
    _configure(ctx, stale_issue_days=20, close_stale_after_days=5,
               auto_unassign_inactive_days=9)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=40)
    issue.labels.append("stale")
    issue.comments.append({
        "body": "⚠️ This issue has been marked **stale** due to inactivity.",
        "created_at": (clock["now"] - timedelta(days=30)).isoformat(),
        "user": {"login": "hiero-bot[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": 12345},
    })
    _wire(mock_gh, issue)
    return clock, issue


def _change_on_call(mock_gh, nth, change):
    """Run ``change`` just before the nth ``get_issue`` answers."""
    real = mock_gh.get_issue.side_effect
    calls = {"n": 0}

    async def get_issue(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == nth:
            change()
        return await real(*args, **kwargs)

    mock_gh.get_issue.side_effect = get_issue


@pytest.mark.parametrize("change", ["remove_label", "add_exempt_label"])
@pytest.mark.asyncio
async def test_close_is_withdrawn_when_a_person_changes_labels_before_it(
    mock_gh, ctx, db, change
):
    clock, issue = _stale_issue(mock_gh, ctx)

    def act():
        if change == "remove_label":
            issue.labels.remove("stale")
        else:
            issue.labels.append("pinned")

    _change_on_call(mock_gh, 2, act)  # 1: the fresh read, 2: just before closing

    counts = await IssueManagementWorkflow(mock_gh).run_stale_scan(
        ctx, now=clock["now"]
    )

    assert counts["closed"] == 0
    mock_gh.close_issue.assert_not_awaited()
    assert issue.state == "open"
    assert await _count(db, PendingIssueCloseNotice) == 0


@pytest.mark.parametrize("change", ["remove_label", "add_exempt_label"])
@pytest.mark.asyncio
async def test_recovery_does_not_close_after_a_person_changed_the_labels(
    mock_gh, ctx, db, change
):
    clock, issue = _stale_issue(mock_gh, ctx)
    mock_gh.close_issue.side_effect = RuntimeError("GitHub temporarily unavailable")
    wf = IssueManagementWorkflow(mock_gh)
    await wf.run_stale_scan(ctx, now=clock["now"])
    assert await _count(db, PendingIssueCloseNotice) == 1

    # The person acts before the pending close's creation time, as in a race
    # the scan's own timeline read did not see.
    if change == "remove_label":
        issue.labels.remove("stale")
        issue.add_timeline_event("unlabeled", _PERSON, label="stale")
    else:
        issue.labels.append("pinned")
    mock_gh.close_issue.side_effect = None
    clock["now"] += timedelta(minutes=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert issue.state == "open"
    assert await _count(db, PendingIssueCloseNotice) == 0
    assert mock_gh.post_comment.await_count == 0


@pytest.mark.asyncio
async def test_event_in_the_same_second_as_the_intent_is_attributed(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)
    clock["now"] = clock["now"].replace(microsecond=650000)
    real = mock_gh.add_label.side_effect

    async def lands_then_crashes(*args, **kwargs):
        await real(*args, **kwargs)
        for event in issue.timeline_events:  # GitHub reports whole seconds
            event["created_at"] = (
                _parse_ts(event["created_at"]).replace(microsecond=0).isoformat()
            )
        raise _Crash()

    mock_gh.add_label.side_effect = lands_then_crashes
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    mock_gh.add_label.side_effect = real
    await db.rollback()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    row = (await db.execute(select(LifecycleAction))).scalars().one()
    await db.refresh(row)
    assert row.status == "delivered"
    assert await _logged_issue_numbers(db) == [1]


@pytest.mark.asyncio
async def test_a_person_acting_in_the_same_second_as_a_pending_close_cancels_it(
    mock_gh, ctx, db
):
    clock, issue = _stale_issue(mock_gh, ctx)
    clock["now"] = clock["now"].replace(microsecond=650000)
    mock_gh.close_issue.side_effect = RuntimeError("GitHub temporarily unavailable")
    wf = IssueManagementWorkflow(mock_gh)
    await wf.run_stale_scan(ctx, now=clock["now"])

    pending = (await db.execute(select(PendingIssueCloseNotice))).scalars().one()
    created = pending.created_at.replace(microsecond=0, tzinfo=UTC)
    issue.timeline_events.append({
        "event": "reopened", "created_at": created.isoformat(),
        "actor": _PERSON, "performed_via_github_app": None,
        "label": None, "assignee": None,
    })
    mock_gh.close_issue.side_effect = None
    await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(minutes=1))

    assert issue.state == "open"
    assert await _count(db, PendingIssueCloseNotice) == 0


@pytest.mark.asyncio
async def test_a_label_a_person_added_first_is_not_credited_to_the_bot(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)

    async def person_wins(owner, repo, number, label, inst):
        issue.labels.append(label)
        issue.add_timeline_event("labeled", _PERSON, label=label)
        issue.touch()

    mock_gh.add_label.side_effect = person_wins
    wf = IssueManagementWorkflow(mock_gh)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert await _logged_issue_numbers(db) == []
    assert not [c for c in issue.comments if "marked **stale**" in c["body"]]
    row = (await db.execute(select(LifecycleAction))).scalars().one()
    assert row.status == "pending" and row.claimed_by is None

    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])
    await db.refresh(row)
    assert row.status == "abandoned"
    assert await _logged_issue_numbers(db) == []


@pytest.mark.asyncio
async def test_an_assignee_a_person_removed_first_is_not_credited_to_the_bot(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx, idle_days=12, assignees=["alice"])

    async def person_wins(owner, repo, number, logins, inst):
        issue.assignees = [a for a in issue.assignees if a not in logins]
        issue.add_timeline_event("unassigned", _PERSON, assignee="alice")
        issue.touch()

    mock_gh.remove_assignees.side_effect = person_wins
    await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    assert await _logged_issue_numbers(db) == []
    assert not [c for c in issue.comments if "unassigned" in c["body"]]


@pytest.mark.asyncio
async def test_a_label_added_between_the_check_and_the_call_withdraws_the_intent(
    mock_gh, ctx, db
):
    clock, issue = _crash_setup(mock_gh, ctx)
    _change_on_call(mock_gh, 2, lambda: issue.labels.append("stale"))

    await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    mock_gh.add_label.assert_not_awaited()
    assert await _count(db, LifecycleAction) == 0


@pytest.mark.asyncio
async def test_retry_clears_a_dead_workers_lease_but_not_a_live_one(
    mock_gh, ctx, db
):
    clock, _ = _crash_setup(mock_gh, ctx)
    wf = IssueManagementWorkflow(mock_gh)
    await wf.run_stale_scan(ctx, now=clock["now"])
    row = (await db.execute(select(LifecycleAction))).scalars().one()
    now = wf._now(ctx)

    row.status, row.claimed_by = "notice_failed", "other"
    row.lease_expires_at = now + timedelta(minutes=10)
    await db.commit()
    assert await wf.retry_failed_notices(ctx) == 0  # a live lease is left alone

    row.lease_expires_at = now - timedelta(minutes=1)
    await db.commit()
    assert await wf.retry_failed_notices(ctx) == 1
    await db.refresh(row)
    assert row.claimed_by is None and row.lease_expires_at is None
    assert row.status == "applied"


@pytest.mark.asyncio
async def test_the_marker_is_searched_even_on_a_first_attempt(mock_gh, ctx, db):
    clock, issue = _crash_setup(mock_gh, ctx)
    real = mock_gh.post_comment.side_effect

    async def accepted_then_crash(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.post_comment.side_effect = accepted_then_crash
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])

    mock_gh.post_comment.side_effect = real
    await db.rollback()
    row = (await db.execute(select(LifecycleAction))).scalars().one()
    row.notice_attempts = 0  # the counter write was lost with the process
    row.claimed_by = None
    await db.commit()
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    assert len([c for c in issue.comments if "marked **stale**" in c["body"]]) == 1


# The last check before a change is the whole decision again.


@pytest.mark.parametrize("change", ["comment", "exempt_label"])
@pytest.mark.parametrize("kind", ["close", "stale", "unassign"])
@pytest.mark.asyncio
async def test_a_person_acting_just_before_the_change_stops_it(
    mock_gh, ctx, db, kind, change
):
    if kind == "close":
        clock, issue = _stale_issue(mock_gh, ctx)
    else:
        idle = 25 if kind == "stale" else 12
        clock, issue = _crash_setup(
            mock_gh, ctx, idle_days=idle,
            assignees=["alice"] if kind == "unassign" else None,
        )

    def act():
        if change == "comment":
            issue.human_comment("Actively working on this.")
        else:
            issue.labels.append("pinned")

    _change_on_call(mock_gh, 2, act)  # 1: the fresh read, 2: the final check

    await IssueManagementWorkflow(mock_gh).run_stale_scan(ctx, now=clock["now"])

    mock_gh.close_issue.assert_not_awaited()
    mock_gh.add_label.assert_not_awaited()
    mock_gh.remove_assignees.assert_not_awaited()
    assert issue.state == "open"
    assert await _count(db, PendingIssueCloseNotice) == 0
    assert await _count(db, LifecycleAction) == 0


@pytest.mark.asyncio
async def test_truncated_timeline_keeps_an_intent_instead_of_abandoning_it(
    mock_gh, ctx, db, monkeypatch
):
    clock, _ = _crash_setup(mock_gh, ctx)
    real = mock_gh.add_label.side_effect

    async def lands_then_crashes(*args, **kwargs):
        await real(*args, **kwargs)
        raise _Crash()

    mock_gh.add_label.side_effect = lands_then_crashes
    wf = IssueManagementWorkflow(mock_gh)
    with pytest.raises(_Crash):
        await wf.run_stale_scan(ctx, now=clock["now"])
    await db.rollback()

    monkeypatch.setattr("app.workflows.issuemanagement._HISTORY_LIMIT", 1)
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])

    row = (await db.execute(select(LifecycleAction))).scalars().one()
    await db.refresh(row)
    assert row.status == "pending" and row.claimed_by is None

    monkeypatch.setattr("app.workflows.issuemanagement._HISTORY_LIMIT", 5000)
    clock["now"] += timedelta(days=1)
    await wf.run_stale_scan(ctx, now=clock["now"])
    await db.refresh(row)
    assert row.status == "delivered"


@pytest.mark.asyncio
async def test_truncated_timeline_keeps_a_pending_close_and_does_not_close(
    mock_gh, ctx, db, monkeypatch
):
    clock, _ = _stale_issue(mock_gh, ctx)
    mock_gh.close_issue.side_effect = RuntimeError("GitHub temporarily unavailable")
    wf = IssueManagementWorkflow(mock_gh)
    await wf.run_stale_scan(ctx, now=clock["now"])
    mock_gh.close_issue.side_effect = None
    mock_gh.close_issue.reset_mock()

    monkeypatch.setattr("app.workflows.issuemanagement._HISTORY_LIMIT", 1)
    counts = await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(minutes=1))

    assert counts["errors"] >= 1
    mock_gh.close_issue.assert_not_awaited()
    assert await _count(db, PendingIssueCloseNotice) == 1


@pytest.mark.asyncio
async def test_truncated_timeline_does_not_attribute_a_close_either_way(
    mock_gh, ctx, db, monkeypatch
):
    clock, issue = _stale_issue(mock_gh, ctx)
    real = mock_gh.close_issue.side_effect

    async def closes_then_times_out(*args, **kwargs):
        await real(*args, **kwargs)
        issue.add_timeline_event(
            "closed", {"login": "hiero-bot[bot]", "type": "Bot"},
            performed_via_github_app={"id": 12345},
        )
        issue.timeline_events[-1]["created_at"] = (
            datetime.now(UTC) + timedelta(minutes=1)
        ).isoformat()
        raise RuntimeError("timeout")

    mock_gh.close_issue.side_effect = closes_then_times_out
    wf = IssueManagementWorkflow(mock_gh)
    await wf.run_stale_scan(ctx, now=clock["now"])

    monkeypatch.setattr("app.workflows.issuemanagement._HISTORY_LIMIT", 1)
    counts = await wf.run_stale_scan(ctx, now=clock["now"] + timedelta(minutes=1))

    assert counts["errors"] >= 1
    pending = (await db.execute(select(PendingIssueCloseNotice))).scalars().one()
    assert pending.closed_at is None
    assert await _logged_issue_numbers(db) == []
