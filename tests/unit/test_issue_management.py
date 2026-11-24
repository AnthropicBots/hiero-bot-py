# tests/unit/test_issue_management.py

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.db.models import PendingIssueCloseNotice, StaleActionLog
from app.workflows.issuemanagement import (
    IssueManagementWorkflow,
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
    async def post_comment(owner, repo, number, body, inst):
        if number == 1:
            raise _http_error(403)

    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(number=1, updated_days_ago=400),
        make_issue(number=2, updated_days_ago=300),
        make_issue(number=3, updated_days_ago=250),
    ])
    mock_gh.post_comment = AsyncMock(side_effect=post_comment)

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
    async def post_comment(owner, repo, number, body, inst):
        if number == 3:
            raise _http_error(403)

    mock_gh.list_issues = AsyncMock(return_value=[
        make_issue(number=1, updated_days_ago=400),
        make_issue(number=2, updated_days_ago=300),
        make_issue(number=3, updated_days_ago=250),
    ])
    mock_gh.post_comment = AsyncMock(side_effect=post_comment)

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
    ):
        self.timeline_events.append({
            "event": event,
            "created_at": self.clock["now"].isoformat(),
            "actor": actor,
            "performed_via_github_app": performed_via_github_app,
            "label": {"name": label} if label is not None else None,
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
        issue.assignees = [a for a in issue.assignees if a not in logins]
        issue.add_timeline_event(
            "unassigned",
            {"login": "hiero-bot[bot]", "type": "Bot"},
            performed_via_github_app={"id": 12345},
        )
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
async def test_persistent_failure_of_the_unassign_notice_never_makes_marking_earlier(
    mock_gh, ctx
):
    # Without the marker the removal's own bump counts as activity: marking is
    # delayed, never brought forward and never skipped.
    _configure(ctx, auto_unassign_inactive_days=9, stale_issue_days=20)
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    issue = _FakeGitHubIssue(clock, idle_days=12, assignees=["sleepy-dev"])
    _wire(mock_gh, issue)
    mock_gh.post_comment.side_effect = RuntimeError("GitHub returned 503")

    marked, _ = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=40
    )

    assert issue.assignees == []
    assert marked is None or marked >= 9


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


@pytest.mark.parametrize(("seconds", "expected"), [(30, 8), (90, 21)])
@pytest.mark.asyncio
async def test_issue_edit_is_attributed_by_the_60_second_window(
    mock_gh, ctx, seconds, expected
):
    # Documents the deliberate limit: an edit leaves no event, so one within a
    # minute of the bot's comment is taken for the bot's own update.
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

    assert marked == expected


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
