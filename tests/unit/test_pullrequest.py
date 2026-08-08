from unittest.mock import AsyncMock

import pytest

<<<<<<< HEAD
from app.workflows.pullrequest import PullRequestWorkflow, QualityCheck


def make_pr(number=1, author="alice"):
    return {
        "number": number,
        "title": "feat: add feature",
        "body": "Closes #123",
        "user": {"login": author},
        "head": {"sha": "abc123"},
=======
from app.workflows.pullrequest import PullRequestWorkflow


def make_pr():
    return {
        "number": 1,
        "title": "Fix duplicate comments",
        "body": "Closes #44",
        "user": {"login": "alice"},
        "head": {
            "sha": "abc123",
            "ref": "fix/pr-synchronize-duplicate-comments",
        },
        "changed_files": 1,
>>>>>>> f16b944 (Add regression test for PR synchronize handling)
        "draft": False,
    }


<<<<<<< HEAD
def make_payload(pr=None):
    return {"pull_request": pr or make_pr()}


@pytest.mark.asyncio
async def test_synchronize_does_not_repeat_ai_review_or_reviewer_recommendation(
    mock_gh, ctx, monkeypatch
):
    cfg = ctx["config"].workflows.pull_request
    cfg.ai_review.enabled = True
    cfg.reviewer_recommendation = True

    wf = PullRequestWorkflow(mock_gh)

    quality_checks = [
        QualityCheck("Linked Issue", True, "PR description references a closing issue")
    ]

    wf._run_quality_checks = AsyncMock(return_value=quality_checks)
    wf._run_ai_review = AsyncMock()
    wf._recommend_reviewers = AsyncMock()

    monkeypatch.setattr(
        "app.workflows.pullrequest.audit.record",
        AsyncMock(),
=======
def make_payload():
    return {"pull_request": make_pr()}


@pytest.mark.asyncio
async def test_opened_then_synchronize_skips_duplicate_reviews(
    mock_gh,
    ctx,
):
    cfg = ctx["config"].workflows.pull_request

    cfg.ai_review.enabled = True
    cfg.reviewer_recommendation = True

    mock_gh.list_pr_files = AsyncMock(
        return_value=[
            {
                "filename": "app/example.py",
                "patch": "+print('test')",
            }
        ]
    )

    wf = PullRequestWorkflow(mock_gh)

    wf._run_ai_review = AsyncMock()
    wf._recommend_reviewers = AsyncMock()

    # Quality checks should run for both events.
    original_quality_checks = wf._run_quality_checks
    wf._run_quality_checks = AsyncMock(
        wraps=original_quality_checks
>>>>>>> f16b944 (Add regression test for PR synchronize handling)
    )

    payload = make_payload()

<<<<<<< HEAD
    # Initial PR opening runs the full PR review pipeline.
    await wf.handle_pr_opened(ctx, payload, "opened")

    wf._run_ai_review.assert_awaited_once()
    wf._recommend_reviewers.assert_awaited_once()
    assert wf._run_quality_checks.await_count == 1

    # A subsequent synchronize event must not repeat AI review
    # or reviewer recommendations.
    await wf.handle_pr_opened(ctx, payload, "synchronize")

    assert wf._run_quality_checks.await_count == 2
    wf._run_ai_review.assert_awaited_once()
    wf._recommend_reviewers.assert_awaited_once()
=======
    # PR opened
    await wf.handle_pr_opened(
        ctx,
        payload,
        "opened",
    )

    # PR synchronized
    await wf.handle_pr_opened(
        ctx,
        payload,
        "synchronize",
    )

    # Quality checks must run for both events.
    assert wf._run_quality_checks.await_count == 2

    # AI review should run only for opened.
    wf._run_ai_review.assert_awaited_once_with(
        ctx,
        payload["pull_request"],
    )

    # Reviewer recommendation should run only for opened.
    wf._recommend_reviewers.assert_awaited_once_with(
        ctx,
        payload["pull_request"],
    )
>>>>>>> f16b944 (Add regression test for PR synchronize handling)
