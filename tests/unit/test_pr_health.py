# tests/unit/test_pr_health.py

import pytest
from unittest.mock import AsyncMock
from app.workflows.prhealth import PRHealthWorkflow


def make_pr(number=1, author="alice", additions=100, deletions=50, body="Closes #1"):
    return {
        "number": number,
        "title": "feat: add feature",
        "body": body,
        "user": {"login": author},
        "head": {"sha": "abc123", "ref": "feat/thing"},
        "additions": additions,
        "deletions": deletions,
        "draft": False,
    }


def make_payload(pr=None):
    return {"pull_request": pr or make_pr()}


@pytest.mark.asyncio
async def test_score_persisted_to_db(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=[
        {"filename": "src/foo.ts", "patch": "+const x = 1;"},
        {"filename": "tests/foo.test.ts", "patch": "+it('works', () => {});"},
    ])
    mock_gh.get_combined_status = AsyncMock(return_value={
        "statuses": [{"context": "DCO", "state": "success"}]
    })
    mock_gh.list_pr_reviews = AsyncMock(return_value=[{"state": "APPROVED"}])

    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload())

    from sqlalchemy import select
    from app.db.models import PRHealthScore
    result = await ctx["db"].execute(select(PRHealthScore))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].pr_number == 1
    assert rows[0].pr_author == "alice"
    assert rows[0].score > 0


@pytest.mark.asyncio
async def test_healthy_label_applied_for_high_score(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=[
        {"filename": "src/foo.ts", "patch": "+x"},
        {"filename": "tests/foo.test.ts", "patch": "+it()"},
    ])
    mock_gh.get_combined_status = AsyncMock(return_value={
        "statuses": [{"context": "DCO", "state": "success"}]
    })
    mock_gh.list_pr_reviews = AsyncMock(return_value=[
        {"state": "APPROVED"}, {"state": "APPROVED"}
    ])

    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload(make_pr(body="Closes #10")))
    mock_gh.add_label.assert_awaited()
    label = mock_gh.add_label.call_args[0][3]
    assert "healthy" in label


@pytest.mark.asyncio
async def test_low_score_posts_comment(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=[
        {"filename": "src/foo.ts", "patch": "+x"}
    ])
    mock_gh.get_combined_status = AsyncMock(return_value={"statuses": []})
    mock_gh.list_pr_reviews = AsyncMock(return_value=[])

    wf = PRHealthWorkflow(mock_gh)
    # PR with no tests, no linked issue, no DCO, short body
    await wf.score_pr(ctx, make_payload(make_pr(body="fix stuff")))
    mock_gh.post_comment.assert_awaited()
    body = mock_gh.post_comment.call_args[0][3]
    assert "Health Score" in body


@pytest.mark.asyncio
async def test_no_comment_above_threshold(mock_gh, ctx):
    mock_gh.list_pr_files = AsyncMock(return_value=[
        {"filename": "src/x.ts", "patch": "+x"},
        {"filename": "tests/x.test.ts", "patch": "+it()"},
    ])
    mock_gh.get_combined_status = AsyncMock(return_value={
        "statuses": [{"context": "DCO", "state": "success"}]
    })
    mock_gh.list_pr_reviews = AsyncMock(return_value=[
        {"state": "APPROVED"}, {"state": "APPROVED"}
    ])

    ctx["config"].workflows.pr_health.comment_threshold = 0  # never comment
    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload(make_pr(body="Closes #5")))
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.pr_health.enabled = False
    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload())
    mock_gh.list_pr_files.assert_not_awaited()


def test_compute_signals_detects_tests():
    files = [{"filename": "tests/test_foo.py"}]
    pr = {"body": "Closes #1", "additions": 50, "deletions": 20}
    signals = PRHealthWorkflow._compute_signals(pr, files, [], {})
    assert signals["has_tests"] is True
    assert signals["has_linked_issue"] is True
    assert signals["small_diff"] is True


def test_compute_signals_large_diff():
    pr = {"body": "", "additions": 300, "deletions": 200}
    signals = PRHealthWorkflow._compute_signals(pr, [], [], {})
    assert signals["small_diff"] is False


def test_score_weights_sum_to_100():
    weights = PRHealthWorkflow.__new__(PRHealthWorkflow)
    from app.config.schema import PRHealthConfig
    cfg = PRHealthConfig()
    total = sum(cfg.score_weights.values())
    assert abs(total - 1.0) < 1e-9


def test_compute_score_all_passing():
    signals = {
        "has_tests": True, "has_linked_issue": True, "has_description": True,
        "dco_signed": True, "review_count": 2, "small_diff": True,
    }
    from app.config.schema import PRHealthConfig
    score = PRHealthWorkflow._compute_score(signals, PRHealthConfig().score_weights)
    assert score == 100.0


def test_compute_score_all_failing():
    signals = {
        "has_tests": False, "has_linked_issue": False, "has_description": False,
        "dco_signed": False, "review_count": 0, "small_diff": False,
    }
    from app.config.schema import PRHealthConfig
    score = PRHealthWorkflow._compute_score(signals, PRHealthConfig().score_weights)
    assert score == 0.0
