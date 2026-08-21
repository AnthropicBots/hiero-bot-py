# tests/unit/test_progression.py

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.workflows.progression import (
    ProgressionWorkflow,
    _months_since,
    _parse_ts,
)


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


# ── Stats collection (#41) ────────────────────────────────────


def iso_days_ago(days):
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_result(total, first_closed_at=None):
    items = [{"closed_at": first_closed_at}] if first_closed_at else []
    return {"total_count": total, "items": items}


@pytest.mark.asyncio
async def test_search_stats_use_total_count(mock_gh):
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(137, iso_days_ago(400)),
            search_result(64),
        ]
    )
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["merged_prs"] == 137
    assert stats["reviews_given"] == 64
    assert stats["months_active"] == 13
    assert stats["source"] == "search"


@pytest.mark.asyncio
async def test_search_stats_query_shape(mock_gh):
    mock_gh.search_issues = AsyncMock(return_value=search_result(0))
    wf = ProgressionWorkflow(mock_gh)

    await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    queries = [call.args[0] for call in mock_gh.search_issues.await_args_list]
    assert "repo:hiero/sdk-js type:pr author:alice is:merged" in queries
    assert "repo:hiero/sdk-js type:pr reviewed-by:alice" in queries


@pytest.mark.asyncio
async def test_no_merged_prs_means_zero_months(mock_gh):
    mock_gh.search_issues = AsyncMock(return_value=search_result(0))
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["merged_prs"] == 0
    assert stats["months_active"] == 0


@pytest.mark.asyncio
async def test_falls_back_to_rest_when_search_fails(mock_gh):
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("rate limited"))
    mock_gh.paginate = AsyncMock(
        side_effect=[
            [
                {"user": {"login": "alice"}, "merged_at": iso_days_ago(200)},
                {"user": {"login": "alice"}, "merged_at": iso_days_ago(90)},
                {"user": {"login": "bob"}, "merged_at": iso_days_ago(10)},
                {"user": {"login": "alice"}, "merged_at": None},
            ],
            [],
        ]
    )
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["source"] == "rest"
    assert stats["merged_prs"] == 2
    assert stats["months_active"] == 6


@pytest.mark.asyncio
async def test_rest_fallback_paginates_full_pr_history(mock_gh):
    """Regression for #41 — the old code only ever read the first 100 closed PRs."""
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(side_effect=[[], []])
    wf = ProgressionWorkflow(mock_gh)

    await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    pulls_call = mock_gh.paginate.await_args_list[0]
    assert pulls_call.args[0] == "/repos/hiero/sdk-js/pulls"
    assert pulls_call.kwargs["params"] == {"state": "closed"}


@pytest.mark.asyncio
async def test_rest_reviews_count_distinct_prs_including_approval_only(mock_gh):
    """Regression for #41 — count submitted reviews, not inline comments."""
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(
        return_value=[
            {"number": 1, "user": {"login": "bob"}},
            {"number": 2, "user": {"login": "bob"}},
            {"number": 3, "user": {"login": "alice"}},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        side_effect=[
            [
                {"user": {"login": "alice"}, "state": "APPROVED"},
            ],
            [
                {"user": {"login": "alice"}, "state": "COMMENTED"},
                {"user": {"login": "alice"}, "state": "APPROVED"},
            ],
            [
                {"user": {"login": "bob"}, "state": "APPROVED"},
            ],
        ]
    )
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["reviews_given"] == 2
    assert mock_gh.list_pr_reviews.await_count == 3


@pytest.mark.asyncio
async def test_rest_fallback_survives_pr_listing_failure(mock_gh):
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(side_effect=[RuntimeError("boom"), []])
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["merged_prs"] == 0
    assert stats["reviews_given"] == 0


@pytest.mark.asyncio
async def test_rest_fallback_survives_review_listing_failure(mock_gh):
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(
        side_effect=[
            [{"number": 1, "user": {"login": "alice"}}],
            [],
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(side_effect=RuntimeError("boom"))
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["reviews_given"] == 0


@pytest.mark.asyncio
async def test_malformed_merge_timestamp_is_ignored(mock_gh):
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(
        side_effect=[
            [{"user": {"login": "alice"}, "merged_at": "not-a-date"}],
            [],
        ]
    )
    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["merged_prs"] == 1
    assert stats["months_active"] == 0


# ── Date helpers ──────────────────────────────────────────────


def test_parse_ts_handles_z_suffix():
    assert _parse_ts("2025-01-10T12:00:00Z") is not None


@pytest.mark.parametrize("value", [None, "", "yesterday", "2025-13-45"])
def test_parse_ts_rejects_junk(value):
    assert _parse_ts(value) is None


def test_months_since_counts_whole_months():
    assert _months_since(datetime.now(timezone.utc) - timedelta(days=95)) == 3


def test_months_since_never_negative():
    assert _months_since(datetime.now(timezone.utc) + timedelta(days=5)) == 0


def test_months_since_of_none_is_zero():
    assert _months_since(None) == 0


def test_months_since_assumes_utc_for_naive_datetimes():
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    assert _months_since(naive) == 2
