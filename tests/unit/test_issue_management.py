# tests/unit/test_issue_management.py

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.db.models import StaleActionLog
from app.workflows.issuemanagement import IssueManagementWorkflow


def make_issue(number=1, updated_days_ago=65, labels=None, assignees=None, is_pr=False):
    updated = (datetime.now(timezone.utc) - timedelta(days=updated_days_ago)).isoformat()
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
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
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
        LabelEscalationRule(label="security", notify_team="sec-team", after_hours=24)
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
    """One issue behaving like GitHub: the bot's own label and comment bump
    ``updated_at``, exactly as they do on the real API."""

    def __init__(self, clock, idle_days: int):
        self.clock = clock
        self.updated = clock["now"] - timedelta(days=idle_days)
        self.labels: list[str] = []
        self.state = "open"

    def touch(self):
        self.updated = self.clock["now"]

    def as_dict(self):
        return {
            "number": 1,
            "updated_at": self.updated.isoformat(),
            "labels": [{"name": name} for name in self.labels],
            "assignees": [],
        }


def _wire(mock_gh, issue: _FakeGitHubIssue):
    async def list_issues(*args, **kwargs):
        return [issue.as_dict()] if issue.state == "open" else []

    async def add_label(owner, repo, number, label, inst):
        issue.labels.append(label)
        issue.touch()

    async def post_comment(owner, repo, number, body, inst):
        issue.touch()

    async def close_issue(owner, repo, number, inst):
        issue.state = "closed"

    mock_gh.list_issues = AsyncMock(side_effect=list_issues)
    mock_gh.add_label = AsyncMock(side_effect=add_label)
    mock_gh.post_comment = AsyncMock(side_effect=post_comment)
    mock_gh.close_issue = AsyncMock(side_effect=close_issue)


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


@pytest.mark.asyncio
async def test_issue_closes_close_stale_after_days_after_being_marked(mock_gh, ctx):
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    issue = _FakeGitHubIssue(clock, idle_days=61)
    _wire(mock_gh, issue)

    marked, closed = await _run_daily(
        IssueManagementWorkflow(mock_gh), ctx, clock, issue, days=120
    )

    assert marked == 0
    assert closed == 7  # close_stale_after_days, not stale_issue_days + 7


@pytest.mark.asyncio
async def test_human_activity_after_marking_delays_the_close(mock_gh, ctx):
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    issue = _FakeGitHubIssue(clock, idle_days=61)
    _wire(mock_gh, issue)
    start = clock["now"]

    def human_comments_on_day_3(day):
        if day == 3:
            issue.updated = start + timedelta(days=3)

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
