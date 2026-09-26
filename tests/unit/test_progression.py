# tests/unit/test_progression.py

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.workflows.progression import (
    MAX_REVIEW_PAGES_PER_PR,
    MAX_REVIEWED_PRS_INSPECTED,
    ProgressionWorkflow,
    _months_since,
    _parse_ts,
    clear_stats_cache,
)


@pytest.fixture(autouse=True)
def _isolate_progression(mock_gh):
    # Stats are cached per (repo, login) at module level; start each test cold.
    clear_stats_cache()
    # The shared fixture grants write access, which now means "already a
    # committer". Progression tests default to an ordinary contributor.
    mock_gh.get_collaborator_permission = AsyncMock(return_value="read")
    yield
    clear_stats_cache()


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
async def test_check_and_report_records_role_suggested_when_eligible(mock_gh, ctx):
    """Issue #92 — /check-eligibility must only log contributor.role_suggested
    when the contributor is actually eligible for a role."""
    from sqlalchemy import select

    from app.db.models import AuditLog

    stats = {"merged_prs": 10, "reviews_given": 5, "months_active": 4, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    payload = {"issue": {"number": 3}, "comment": {"user": {"login": "alice"}}}
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.check_and_report(ctx, payload)

    result = await ctx["db"].execute(select(AuditLog))
    entry = result.scalars().one()
    assert entry.action == "contributor.role_suggested"


@pytest.mark.asyncio
async def test_check_and_report_records_workflow_skipped_when_not_eligible(mock_gh, ctx):
    """A contributor nowhere near eligible must not be logged as
    contributor.role_suggested — that was the bug in issue #92."""
    from sqlalchemy import select

    from app.db.models import AuditLog

    stats = {"merged_prs": 0, "reviews_given": 0, "months_active": 0, "login": "bob"}
    wf = ProgressionWorkflow(mock_gh)
    payload = {"issue": {"number": 4}, "comment": {"user": {"login": "bob"}}}
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.check_and_report(ctx, payload)

    result = await ctx["db"].execute(select(AuditLog))
    entry = result.scalars().one()
    assert entry.action == "workflow.skipped"


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
async def test_search_stats_use_search_for_candidates(mock_gh):
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(137, iso_days_ago(400)),
            {"total_count": 2, "items": [{"number": 101}, {"number": 102}]},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        side_effect=[
            [
                {"user": {"login": "alice"}, "state": "APPROVED"},
                {"user": {"login": "alice"}, "state": "COMMENTED"},
            ],
            [
                {"user": {"login": "alice"}, "state": "APPROVED"},
            ],
        ]
    )

    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["merged_prs"] == 137
    assert stats["reviews_given"] == 3
    assert stats["months_active"] == 13
    assert stats["source"] == "search"
    assert stats["partial"] is False
    assert mock_gh.list_pr_reviews.await_count == 2


@pytest.mark.asyncio
async def test_search_stats_query_shape(mock_gh):
    mock_gh.search_issues = AsyncMock(
        return_value=search_result(0)
    )
    mock_gh.list_pr_reviews = AsyncMock(return_value=[])

    wf = ProgressionWorkflow(mock_gh)

    await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    queries = [
        call.args[0] for call in mock_gh.search_issues.await_args_list
    ]
    assert queries == [
        "repo:hiero/sdk-js type:pr author:alice is:merged",
        "repo:hiero/sdk-js type:pr reviewed-by:alice",
    ]
    reviewed_call = mock_gh.search_issues.await_args_list[1]
    assert reviewed_call.kwargs["per_page"] == MAX_REVIEWED_PRS_INSPECTED


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
                {"number": 1, "user": {"login": "alice"}, "merged_at": iso_days_ago(200)},
                {"number": 2, "user": {"login": "alice"}, "merged_at": iso_days_ago(90)},
                {"number": 3, "user": {"login": "bob"}, "merged_at": iso_days_ago(10)},
                {"number": 4, "user": {"login": "alice"}, "merged_at": None},
            ],
            [
                {"number": 1},
                {"number": 2},
                {"number": 3},
                {"number": 4},
            ],
        ]
    )

    mock_gh.list_pr_reviews = AsyncMock(
        side_effect=[
            [{"user": {"login": "alice"}, "state": "APPROVED"}],
            [{"user": {"login": "bob"}, "state": "APPROVED"}],
            [{"user": {"login": "alice"}, "state": "COMMENTED"}],
            [],
        ]
    )

    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["source"] == "rest"
    assert stats["merged_prs"] == 2
    assert stats["reviews_given"] == 2
    assert stats["months_active"] == 6


@pytest.mark.asyncio
async def test_rest_fallback_uses_paginated_pr_history(mock_gh):
    """Regression for #41 — contributor PRs outside page one are included."""
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))

    recent_prs = [
        {
            "number": number,
            "user": {"login": "bob"},
            "merged_at": iso_days_ago(10),
        }
        for number in range(1, 101)
    ]
    older_pr = {
        "number": 101,
        "user": {"login": "alice"},
        "merged_at": iso_days_ago(200),
    }

    mock_gh.paginate = AsyncMock(
        side_effect=[
            recent_prs + [older_pr],
            recent_prs + [older_pr],
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(return_value=[])

    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["merged_prs"] == 1
    assert stats["months_active"] == 6


@pytest.mark.asyncio
async def test_rest_reviews_count_submitted_reviews_including_approval_only(mock_gh):
    """Regression for #41 — count submitted reviews, not inline comments or PRs."""
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

    assert stats["reviews_given"] == 3
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
async def test_rest_fallback_keeps_reviews_found_before_partial_failure(mock_gh):
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(
        return_value=[
            {"number": 1, "user": {"login": "bob"}},
            {"number": 2, "user": {"login": "bob"}},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        side_effect=[
            [{"user": {"login": "alice"}, "state": "APPROVED"}],
            RuntimeError("boom"),
        ]
    )

    wf = ProgressionWorkflow(mock_gh)

    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["reviews_given"] == 1
    assert mock_gh.list_pr_reviews.await_count == 2


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


# ── Bounded GitHub calls (#122) ───────────────────────────────


@pytest.mark.asyncio
async def test_search_path_bounds_calls_for_a_large_review_history(mock_gh):
    """A reviewer of ~300 PRs used to cost ~300 review fetches."""
    reviewed = [{"number": n} for n in range(1, MAX_REVIEWED_PRS_INSPECTED + 1)]
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(40, iso_days_ago(400)),
            {"total_count": 300, "items": reviewed},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        return_value=[{"user": {"login": "alice"}, "state": "APPROVED"}] * 2
    )

    wf = ProgressionWorkflow(mock_gh)
    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert mock_gh.search_issues.await_count == 2
    assert mock_gh.list_pr_reviews.await_count == MAX_REVIEWED_PRS_INSPECTED
    # Inspected PRs are counted exactly; the other 250 count once each.
    assert stats["reviews_given"] == MAX_REVIEWED_PRS_INSPECTED * 2 + 250
    assert stats["partial"] is True


@pytest.mark.asyncio
async def test_search_path_counts_unreadable_reviewed_pr_once(mock_gh):
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(1, iso_days_ago(40)),
            {"total_count": 2, "items": [{"number": 1}, {"number": 2}]},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        side_effect=[
            [{"user": {"login": "alice"}, "state": "APPROVED"}] * 3,
            RuntimeError("boom"),
        ]
    )

    wf = ProgressionWorkflow(mock_gh)
    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["source"] == "search"
    assert stats["reviews_given"] == 4
    # One PR's count is a guess, so the total is flagged as a lower bound.
    assert stats["partial"] is True


@pytest.mark.asyncio
async def test_rest_fallback_is_bounded_on_a_large_history(mock_gh):
    """The REST fallback on a 2,000-PR repo used to cost ~2,000 calls."""
    from app.workflows.progression import MAX_REST_PR_PAGES

    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("rate limited"))
    history = [
        {"number": n, "user": {"login": "bob"}, "merged_at": iso_days_ago(10)}
        for n in range(1, 2001)
    ]

    async def paginate(path, inst, *, params=None, per_page=100, max_pages=50, **_):
        return history[: per_page * max_pages]

    mock_gh.paginate = AsyncMock(side_effect=paginate)
    mock_gh.list_pr_reviews = AsyncMock(return_value=[])

    wf = ProgressionWorkflow(mock_gh)
    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    for call in mock_gh.paginate.await_args_list:
        assert call.kwargs["max_pages"] <= MAX_REST_PR_PAGES
    assert mock_gh.list_pr_reviews.await_count <= MAX_REVIEWED_PRS_INSPECTED
    assert stats["source"] == "rest"
    assert stats["partial"] is True


@pytest.mark.asyncio
async def test_repeated_check_eligibility_uses_cached_stats(mock_gh, ctx):
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(3, iso_days_ago(60)),
            {"total_count": 1, "items": [{"number": 7}]},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        return_value=[{"user": {"login": "alice"}, "state": "APPROVED"}]
    )
    payload = {"issue": {"number": 3}, "comment": {"user": {"login": "alice"}}}

    wf = ProgressionWorkflow(mock_gh)
    await wf.check_and_report(ctx, payload)
    await ProgressionWorkflow(mock_gh).check_and_report(ctx, payload)

    assert mock_gh.search_issues.await_count == 2
    assert mock_gh.list_pr_reviews.await_count == 1
    assert mock_gh.post_comment.await_count == 2


@pytest.mark.asyncio
async def test_merge_does_not_reuse_cached_stats(mock_gh, ctx):
    """Cached counts would repeat or miss milestones on back-to-back merges."""
    wf = ProgressionWorkflow(mock_gh)
    collect = AsyncMock(return_value={
        "merged_prs": 2, "reviews_given": 0, "months_active": 0, "login": "alice",
    })
    with patch.object(wf, "_collect_stats", collect):
        await wf.handle_merged_pr(ctx, merged_pr_payload())
    assert collect.await_args.kwargs["use_cache"] is False


def test_partial_stats_are_flagged_in_report():
    from app.config.schema import ProgressionConfig

    stats = {"merged_prs": 1, "reviews_given": 400, "months_active": 2, "partial": True}
    report = ProgressionWorkflow._build_full_report("alice", stats, ProgressionConfig())
    assert "lower bound" in report


# ── Bot authors and repeated notices (#122) ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [
    {"login": "dependabot[bot]", "type": "Bot"},
    {"login": "dependabot[bot]"},
    {"login": "some-app", "type": "Bot"},
])
async def test_bot_authored_merge_gets_no_comment_or_snapshot(mock_gh, ctx, user):
    from sqlalchemy import select

    from app.db.models import AuditLog, ContributorSnapshot

    payload = merged_pr_payload()
    payload["pull_request"]["user"] = user
    wf = ProgressionWorkflow(mock_gh)
    collect = AsyncMock()
    with patch.object(wf, "_collect_stats", collect):
        await wf.handle_merged_pr(ctx, payload)

    collect.assert_not_awaited()
    mock_gh.post_comment.assert_not_awaited()
    db = ctx["db"]
    assert (await db.execute(select(ContributorSnapshot))).scalars().all() == []
    assert (await db.execute(select(AuditLog))).scalars().all() == []


def eligibility_notices(mock_gh):
    return [c for c in all_comments(mock_gh) if "may now be eligible" in c]


@pytest.mark.asyncio
async def test_second_qualifying_merge_does_not_repeat_notice(mock_gh, ctx):
    stats = {"merged_prs": 5, "reviews_given": 3, "months_active": 3, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload(pr_number=5))
        await wf.handle_merged_pr(ctx, merged_pr_payload(pr_number=6))

    assert len(eligibility_notices(mock_gh)) == 1


@pytest.mark.asyncio
async def test_next_role_is_still_announced_after_an_earlier_one(mock_gh, ctx):
    wf = ProgressionWorkflow(mock_gh)
    junior = {"merged_prs": 5, "reviews_given": 3, "months_active": 3, "login": "alice"}
    committer = {"merged_prs": 20, "reviews_given": 12, "months_active": 8, "login": "alice"}
    with patch.object(wf, "_collect_stats", AsyncMock(side_effect=[junior, committer])):
        await wf.handle_merged_pr(ctx, merged_pr_payload(pr_number=5))
        await wf.handle_merged_pr(ctx, merged_pr_payload(pr_number=6))

    notices = eligibility_notices(mock_gh)
    assert len(notices) == 2
    assert "**committer**" in notices[1]


@pytest.mark.asyncio
async def test_notice_deduplicated_against_legacy_audit_entry(mock_gh, ctx):
    """Entries written before #122 carry the role only in the reason."""
    from app.utils import audit

    await audit.record(
        ctx["db"], action="contributor.role_suggested",
        owner="hiero", repo="sdk-js", target_login="alice", target_number=1,
        reason="Post-merge check: eligible_for=junior-committer",
        metadata={"merged_prs": 3},
    )
    stats = {"merged_prs": 5, "reviews_given": 3, "months_active": 3, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())

    assert eligibility_notices(mock_gh) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("permission, role", [
    ("admin", "maintainer"),
    ("maintain", "maintainer"),
    ("write", "committer"),
])
async def test_existing_role_holder_is_not_told_they_are_eligible(
    mock_gh, ctx, permission, role
):
    from sqlalchemy import select

    from app.db.models import ContributorSnapshot

    mock_gh.get_collaborator_permission = AsyncMock(return_value=permission)
    stats = {"merged_prs": 20, "reviews_given": 12, "months_active": 8, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())

    assert eligibility_notices(mock_gh) == []
    snapshot = (await ctx["db"].execute(select(ContributorSnapshot))).scalars().one()
    assert snapshot.current_role == role
    assert snapshot.eligible_for is None


@pytest.mark.asyncio
async def test_permission_lookup_failure_treats_author_as_contributor(mock_gh, ctx):
    mock_gh.get_collaborator_permission = AsyncMock(side_effect=RuntimeError("404"))
    stats = {"merged_prs": 5, "reviews_given": 3, "months_active": 3, "login": "alice"}
    wf = ProgressionWorkflow(mock_gh)
    with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
        await wf.handle_merged_pr(ctx, merged_pr_payload())

    assert len(eligibility_notices(mock_gh)) == 1


# ── Review feedback on #122 ───────────────────────────────────


@pytest.mark.asyncio
async def test_review_pages_per_pr_are_capped(mock_gh):
    """Each PR's review listing is bounded, not just the number of PRs."""
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(1, iso_days_ago(40)),
            {"total_count": 1, "items": [{"number": 1}]},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(return_value=[])

    wf = ProgressionWorkflow(mock_gh)
    await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert mock_gh.list_pr_reviews.await_args.kwargs["max_pages"] == MAX_REVIEW_PAGES_PER_PR


@pytest.mark.asyncio
async def test_pr_with_reviews_past_the_page_cap_marks_stats_partial(mock_gh):
    mock_gh.search_issues = AsyncMock(
        side_effect=[
            search_result(1, iso_days_ago(40)),
            {"total_count": 1, "items": [{"number": 1}]},
        ]
    )
    mock_gh.list_pr_reviews = AsyncMock(
        return_value=[{"user": {"login": "bob"}}] * (MAX_REVIEW_PAGES_PER_PR * 100)
    )

    wf = ProgressionWorkflow(mock_gh)
    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["partial"] is True


@pytest.mark.asyncio
async def test_rest_fallback_marks_partial_when_a_review_fetch_fails(mock_gh):
    mock_gh.search_issues = AsyncMock(side_effect=RuntimeError("no search"))
    mock_gh.paginate = AsyncMock(side_effect=[[], [{"number": 1}, {"number": 2}]])
    mock_gh.list_pr_reviews = AsyncMock(
        side_effect=[
            [{"user": {"login": "alice"}, "state": "APPROVED"}],
            RuntimeError("boom"),
        ]
    )

    wf = ProgressionWorkflow(mock_gh)
    stats = await wf._collect_stats("hiero", "sdk-js", "alice", 42)

    assert stats["reviews_given"] == 1
    assert stats["partial"] is True


@pytest.mark.asyncio
async def test_stats_cache_never_grows_past_its_limit(mock_gh, monkeypatch):
    from app.workflows import progression

    monkeypatch.setattr(progression, "_MAX_STATS_CACHE_ENTRIES", 3)
    mock_gh.search_issues = AsyncMock(return_value=search_result(0))

    wf = ProgressionWorkflow(mock_gh)
    for login in ["alice", "bob", "carol", "dave", "erin"]:
        await wf._collect_stats("hiero", "sdk-js", login, 42)

    # Only the three most recent logins are kept; the oldest were evicted.
    assert [key[2] for key in progression._stats_cache] == ["carol", "dave", "erin"]


@pytest.mark.asyncio
async def test_concurrent_merges_post_a_single_eligibility_notice(
    mock_gh, base_config, tmp_path
):
    """Two merge webhooks for one contributor run at once, each with its own
    DB session, as they would in production."""
    import asyncio

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.db.database import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def slow_comment(*args, **kwargs):
        # Give the other webhook a chance to interleave between the audit-log
        # check and the audit-row commit.
        await asyncio.sleep(0.05)

    mock_gh.post_comment = AsyncMock(side_effect=slow_comment)
    stats = {"merged_prs": 5, "reviews_given": 3, "months_active": 3, "login": "alice"}

    async def merge(pr_number):
        async with factory() as session:
            ctx = {
                "owner": "hiero", "repo": "sdk-js", "installation_id": 42,
                "config": base_config, "db": session,
            }
            wf = ProgressionWorkflow(mock_gh)
            with patch.object(wf, "_collect_stats", AsyncMock(return_value=stats)):
                await wf.handle_merged_pr(ctx, merged_pr_payload(pr_number=pr_number))

    try:
        await asyncio.gather(merge(5), merge(6))
    finally:
        await engine.dispose()

    assert len(eligibility_notices(mock_gh)) == 1
