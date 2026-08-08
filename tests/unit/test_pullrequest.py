from unittest.mock import AsyncMock

import pytest

from app.workflows.pullrequest import PullRequestWorkflow, QualityCheck


def make_pr(number=1, author="alice"):
    return {
        "number": number,
        "title": "feat: add feature",
        "body": "Closes #123",
        "user": {"login": author},
        "head": {"sha": "abc123"},
        "draft": False,
    }


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
    )

    payload = make_payload()

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