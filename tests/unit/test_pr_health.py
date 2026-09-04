# tests/unit/test_pr_health.py

from unittest.mock import AsyncMock

import pytest

from app.workflows.prhealth import LABEL_NEEDS_WORK, PRHealthWorkflow


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
async def test_needs_work_label_posts_comment_for_boundary_score(mock_gh, ctx):
    """
    Regression test for issue #47: a PR whose score falls between
    comment_threshold and label_healthy_above must still get both the
    LABEL_NEEDS_WORK label AND the explanatory comment. Previously only
    `score < comment_threshold` triggered a comment, so a PR scoring in
    this gap got an unexplained "needs work" label with no comment.

    Rather than trusting the fixture lands in the gap by luck, this test
    recomputes the score with the workflow's own static methods (same
    inputs used above) and asserts it is *actually* in the boundary,
    so the test stays meaningful if score_weights or thresholds change.
    """
    files = [
        {"filename": "tests/test_feature.py", "patch": "+def test_case(): pass"}
    ]
    reviews = []
    status = {"statuses": []}

    mock_gh.list_pr_files = AsyncMock(return_value=files)
    mock_gh.get_combined_status = AsyncMock(return_value=status)
    mock_gh.list_pr_reviews = AsyncMock(return_value=reviews)

    wf = PRHealthWorkflow(mock_gh)
    body = (
        "Closes #5\n\nThis PR adds the requested feature and includes "
        "enough detail for reviewers to understand the scope."
    )
    pr = make_pr(body=body)
    await wf.score_pr(ctx, make_payload(pr))

    cfg = ctx["config"].workflows.pr_health
    signals = PRHealthWorkflow._compute_signals(pr, files, reviews, status)
    score = PRHealthWorkflow._compute_score(signals, cfg.score_weights)

    assert cfg.comment_threshold <= score < cfg.label_healthy_above, (
        f"fixture score {score} is not between comment_threshold="
        f"{cfg.comment_threshold} and label_healthy_above="
        f"{cfg.label_healthy_above}; this test no longer covers the "
        "boundary case from issue #47 — adjust the PR/files/reviews "
        "fixture above so the score lands back in that gap."
    )

    label = mock_gh.add_label.call_args[0][3]
    assert label == LABEL_NEEDS_WORK
    mock_gh.post_comment.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_score_pr_detects_test_file_from_second_page(mock_gh, ctx):
    first_page = [{"filename": f"src/file_{i}.py"} for i in range(100)]
    second_page = [{"filename": "tests/test_feature.py"}]

    mock_gh.list_pr_files = AsyncMock(
        return_value=first_page + second_page
    )
    mock_gh.get_combined_status = AsyncMock(return_value={})
    mock_gh.list_pr_reviews = AsyncMock(return_value=[])

    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload())

    from sqlalchemy import select

    from app.db.models import PRHealthScore

    result = await ctx["db"].execute(select(PRHealthScore))
    row = result.scalars().one()

    assert row.files_changed == 101
    assert row.has_tests is True


def test_compute_signals_large_diff():
    pr = {"body": "", "additions": 300, "deletions": 200}
    signals = PRHealthWorkflow._compute_signals(pr, [], [], {})
    assert signals["small_diff"] is False


def test_score_weights_sum_to_100():
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