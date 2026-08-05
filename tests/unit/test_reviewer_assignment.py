from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
import yaml

from app.workflows.reviewerassignment import ReviewerAssignmentWorkflow


def make_pr(number=1, author="alice"):
    return {
        "number": number,
        "title": "feat: add feature",
        "body": "",
        "user": {"login": author},
        "head": {"sha": "abc123"},
        "draft": False,
    }


def make_payload(pr=None):
    return {"pull_request": pr or make_pr()}


def reviewers_file(reviewers):
    return base64.b64encode(
        yaml.safe_dump({"reviewers": reviewers}).encode()
    ).decode()


@pytest.mark.asyncio
async def test_skips_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.reviewer_assignment.enabled = False

    wf = ReviewerAssignmentWorkflow(mock_gh)
    await wf.handle_pr_opened(ctx, make_payload())

    mock_gh.request_reviewers.assert_not_awaited()


@pytest.mark.asyncio
async def test_assigns_available_reviewer(mock_gh, ctx):
    ctx["config"].workflows.reviewer_assignment.enabled = True

    mock_gh.get_file_content = AsyncMock(
        return_value=reviewers_file(
            [
                {"login": "bob", "available": True},
            ]
        )
    )

    wf = ReviewerAssignmentWorkflow(mock_gh)

    await wf.handle_pr_opened(ctx, make_payload())

    mock_gh.request_reviewers.assert_awaited_once()

    reviewers = mock_gh.request_reviewers.call_args.args[3]

    assert reviewers == ["bob"]


@pytest.mark.asyncio
async def test_skips_unavailable_reviewers(mock_gh, ctx):
    ctx["config"].workflows.reviewer_assignment.enabled = True

    mock_gh.get_file_content = AsyncMock(
        return_value=reviewers_file(
            [
                {"login": "bob", "available": False},
                {"login": "charlie", "available": True},
            ]
        )
    )

    wf = ReviewerAssignmentWorkflow(mock_gh)

    await wf.handle_pr_opened(ctx, make_payload())

    reviewers = mock_gh.request_reviewers.call_args.args[3]

    assert reviewers == ["charlie"]


@pytest.mark.asyncio
async def test_excludes_pr_author(mock_gh, ctx):
    ctx["config"].workflows.reviewer_assignment.enabled = True

    mock_gh.get_file_content = AsyncMock(
        return_value=reviewers_file(
            [
                {"login": "alice", "available": True},
                {"login": "bob", "available": True},
            ]
        )
    )

    wf = ReviewerAssignmentWorkflow(mock_gh)

    await wf.handle_pr_opened(ctx, make_payload())

    reviewers = mock_gh.request_reviewers.call_args.args[3]

    assert reviewers == ["bob"]
