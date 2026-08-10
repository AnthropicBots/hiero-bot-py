# tests/unit/test_reviewer_recommendation.py — PR reviewer suggestions (#45)

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.models import ReviewerRecommendation
from app.workflows.pullrequest import (
    MAX_PATHS_QUERIED,
    PullRequestWorkflow,
    _score_candidates,
    _touched_directories,
)


def pr(number=7, author="alice"):
    return {"number": number, "user": {"login": author}}


def files(*names):
    return [{"filename": name} for name in names]


def commits(*logins):
    return [{"author": {"login": login}} for login in logins]


def suggestion_bodies(mock_gh):
    return [
        call[0][3]
        for call in mock_gh.post_comment.call_args_list
        if "Suggested reviewers" in call[0][3]
    ]


# ── Directory extraction ──────────────────────────────────────


def test_directories_ranked_by_change_count():
    changed = files(
        "app/github/client.py",
        "app/github/webhooks.py",
        "tests/unit/test_x.py",
    )
    assert _touched_directories(changed)[0] == "app/github"


def test_root_level_files_are_dropped():
    assert _touched_directories(files("README.md", "setup.py")) == []


def test_directory_list_is_capped():
    changed = files(*[f"pkg{i}/mod.py" for i in range(20)])
    assert len(_touched_directories(changed)) == MAX_PATHS_QUERIED


def test_missing_filename_is_tolerated():
    assert _touched_directories([{}, {"filename": "app/x.py"}]) == ["app"]


# ── Scoring ───────────────────────────────────────────────────


def test_scores_count_commits_per_author():
    scores = _score_candidates([commits("bob", "bob", "carol")], exclude="alice")
    assert scores == {"bob": 2, "carol": 1}


def test_pr_author_is_excluded():
    scores = _score_candidates([commits("alice", "bob")], exclude="alice")
    assert "alice" not in scores


def test_bot_committers_are_excluded():
    scores = _score_candidates(
        [commits("dependabot[bot]", "bob")], exclude="alice"
    )
    assert scores == {"bob": 1}


def test_failed_history_lookup_does_not_break_scoring():
    scores = _score_candidates(
        [RuntimeError("403"), commits("bob")], exclude="alice"
    )
    assert scores == {"bob": 1}


def test_commits_without_a_linked_account_are_skipped():
    scores = _score_candidates(
        [[{"author": None}, {"author": {"login": ""}}, *commits("bob")]],
        exclude="alice",
    )
    assert scores == {"bob": 1}


# ── End-to-end ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_request_per_directory_not_per_pr(mock_gh, ctx):
    """Regression for #45 — this used to cost up to 51 calls per opened PR."""
    mock_gh.list_pr_files = AsyncMock(
        return_value=files("app/github/client.py", "app/db/models.py")
    )
    mock_gh.list_commits = AsyncMock(return_value=commits("bob"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr())

    assert mock_gh.list_commits.await_count == 2
    assert mock_gh.list_pr_files.await_count == 1


@pytest.mark.asyncio
async def test_suggests_the_most_active_committers(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=files("app/github/client.py"))
    mock_gh.list_commits = AsyncMock(
        return_value=commits("bob", "bob", "bob", "carol", "dave")
    )

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr())

    body = suggestion_bodies(mock_gh)[0]
    assert "@bob" in body
    assert "@carol" in body
    assert "@dave" not in body


@pytest.mark.asyncio
async def test_recommendations_are_persisted(mock_gh, ctx, db):
    mock_gh.list_pr_files = AsyncMock(return_value=files("app/github/client.py"))
    mock_gh.list_commits = AsyncMock(return_value=commits("bob", "bob", "carol"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr(number=7))
    await db.commit()

    rows = (await db.execute(select(ReviewerRecommendation))).scalars().all()

    assert {row.recommended_reviewer for row in rows} == {"bob", "carol"}
    assert all(row.pr_number == 7 for row in rows)
    top = next(row for row in rows if row.recommended_reviewer == "bob")
    assert top.score == 1.0
    assert "app/github" in top.reason


@pytest.mark.asyncio
async def test_no_comment_when_nobody_qualifies(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=files("app/github/client.py"))
    mock_gh.list_commits = AsyncMock(return_value=commits("alice"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr(author="alice"))

    assert suggestion_bodies(mock_gh) == []


@pytest.mark.asyncio
async def test_root_only_pr_makes_no_history_calls(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=files("README.md"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr())

    mock_gh.list_commits.assert_not_awaited()
    assert suggestion_bodies(mock_gh) == []


@pytest.mark.asyncio
async def test_history_failure_is_swallowed(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=files("app/x.py"))
    mock_gh.list_commits = AsyncMock(side_effect=RuntimeError("403"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr())

    assert suggestion_bodies(mock_gh) == []


@pytest.mark.asyncio
async def test_file_listing_failure_is_swallowed(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(side_effect=RuntimeError("boom"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr())

    assert suggestion_bodies(mock_gh) == []


@pytest.mark.asyncio
async def test_audit_records_the_call_budget(mock_gh, ctx, db):
    from app.db.models import AuditLog

    mock_gh.list_pr_files = AsyncMock(
        return_value=files("app/a/x.py", "app/b/y.py")
    )
    mock_gh.list_commits = AsyncMock(return_value=commits("bob"))

    wf = PullRequestWorkflow(mock_gh)
    await wf._recommend_reviewers(ctx, pr())
    await db.commit()

    entry = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "pr.reviewer_recommended")
        )
    ).scalar_one()

    assert entry.metadata_json["api_calls"] == 3
    assert entry.metadata_json["directories"] == ["app/a", "app/b"]
