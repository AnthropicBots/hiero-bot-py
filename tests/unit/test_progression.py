# tests/unit/test_progression.py

import pytest
from unittest.mock import AsyncMock, patch
from app.workflows.progression import ProgressionWorkflow


def merged_pr_payload(login="alice", pr_number=5):
    return {
        "pull_request": {
            "number": pr_number,
            "user": {"login": login},
            "merged_at": "2025-01-10T12:00:00Z",
        }
    }


def all_comments(mock_gh):
    """Return list of all post_comment bodies."""
    return [c[0][3] for c in mock_gh.post_comment.call_args_list]


@pytest.mark.asyncio
async def test_recommends_issues_after_merge(mock_gh, ctx):
    stats = {"merged_prs": 3, "reviews_given": 2, "months_active": 1, "login": "alice"}
    mock_gh.list_issues = AsyncMock(return_value=[
        {"number": 10, "title": "Fix X",
         "html_url": "https://github.com/hiero/sdk/issues/10",
         "assignees": [], "pull_request": None},
    ])
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())
    assert any("suggested next issues" in c for c in all_comments(mock_gh))


@pytest.mark.asyncio
async def test_celebrates_first_pr_milestone(mock_gh, ctx):
    stats = {"merged_prs": 1, "reviews_given": 0, "months_active": 0, "login": "alice"}
    mock_gh.list_issues = AsyncMock(return_value=[])
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload(pr_number=1))
    assert any("First merged PR" in c for c in all_comments(mock_gh))


@pytest.mark.asyncio
async def test_celebrates_tenth_pr_milestone(mock_gh, ctx):
    stats = {"merged_prs": 10, "reviews_given": 5, "months_active": 3, "login": "alice"}
    mock_gh.list_issues = AsyncMock(return_value=[])
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())
    assert any("10 merged PRs" in c for c in all_comments(mock_gh))


@pytest.mark.asyncio
async def test_no_milestone_for_non_milestone_count(mock_gh, ctx):
    stats = {"merged_prs": 7, "reviews_given": 2, "months_active": 2, "login": "alice"}
    mock_gh.list_issues = AsyncMock(return_value=[])
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())
    comments = all_comments(mock_gh)
    milestone_comments = [c for c in comments if any(e in c for e in ["🎊", "🌟", "🚀", "💎", "🏆"])]
    assert len(milestone_comments) == 0


@pytest.mark.asyncio
async def test_skips_when_not_merged(mock_gh, ctx):
    wf = ProgressionWorkflow(mock_gh)
    await wf.handle_merged_pr(ctx, {
        "pull_request": {"number": 1, "user": {"login": "alice"}, "merged_at": None}
    })
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.progression.enabled = False
    wf = ProgressionWorkflow(mock_gh)
    await wf.handle_merged_pr(ctx, merged_pr_payload())
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_report_posts_table(mock_gh, ctx):
    stats = {"merged_prs": 10, "reviews_given": 5, "months_active": 4, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    payload = {"issue": {"number": 3}, "comment": {"user": {"login": "alice"}}}
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.check_and_report(ctx, payload)
    mock_gh.post_comment.assert_awaited_once()
    body = mock_gh.post_comment.call_args[0][3]
    assert "Progression Report" in body
    assert "junior-committer" in body
    assert "committer" in body


@pytest.mark.asyncio
async def test_eligible_role_announced_after_merge(mock_gh, ctx):
    stats = {"merged_prs": 5, "reviews_given": 3, "months_active": 3, "login": "alice"}
    mock_gh.list_issues = AsyncMock(return_value=[])
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())
    assert any("junior-committer" in c for c in all_comments(mock_gh))


@pytest.mark.asyncio
async def test_no_issue_recommendations_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.progression.recommend_issues_after_merge = False
    stats = {"merged_prs": 3, "reviews_given": 2, "months_active": 2, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())
    assert not any("suggested next issues" in c for c in all_comments(mock_gh))


def test_check_eligibility_not_eligible():
    from app.config.schema import ProgressionConfig
    cfg = ProgressionConfig()
    assert ProgressionWorkflow._check_eligibility(
        {"merged_prs": 1, "reviews_given": 0, "months_active": 0}, cfg
    ) is None


def test_check_eligibility_junior_committer():
    from app.config.schema import ProgressionConfig
    cfg = ProgressionConfig()
    assert ProgressionWorkflow._check_eligibility(
        {"merged_prs": 5, "reviews_given": 3, "months_active": 2}, cfg
    ) == "junior-committer"


def test_check_eligibility_committer():
    from app.config.schema import ProgressionConfig
    cfg = ProgressionConfig()
    assert ProgressionWorkflow._check_eligibility(
        {"merged_prs": 20, "reviews_given": 12, "months_active": 8}, cfg
    ) == "committer"


def test_check_eligibility_maintainer():
    from app.config.schema import ProgressionConfig
    cfg = ProgressionConfig()
    assert ProgressionWorkflow._check_eligibility(
        {"merged_prs": 55, "reviews_given": 35, "months_active": 14}, cfg
    ) == "maintainer"
