from unittest.mock import AsyncMock

import pytest

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
        "draft": False,
    }


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

    original_quality_checks = wf._run_quality_checks
    wf._run_quality_checks = AsyncMock(
        wraps=original_quality_checks
    )

    payload = make_payload()

    await wf.handle_pr_opened(
        ctx,
        payload,
        "opened",
    )

    await wf.handle_pr_opened(
        ctx,
        payload,
        "synchronize",
    )

    assert wf._run_quality_checks.await_count == 2

    wf._run_ai_review.assert_awaited_once_with(
        ctx,
        payload["pull_request"],
    )

    wf._recommend_reviewers.assert_awaited_once_with(
        ctx,
        payload["pull_request"],
    )
