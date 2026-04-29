# tests/unit/test_issue_management.py

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from app.workflows.issuemanagement import IssueManagementWorkflow


def make_issue(number=1, updated_days_ago=65, labels=None, assignees=None, is_pr=False):
    updated = (datetime.now(timezone.utc) - timedelta(days=updated_days_ago)).isoformat()
    issue = {
        "number": number,
        "title": f"Issue #{number}",
        "updated_at": updated,
        "labels": [{"name": l} for l in (labels or [])],
        "assignees": [{"login": a} for a in (assignees or [])],
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
async def test_closes_after_stale_plus_close_period(mock_gh, ctx):
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
