from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
import yaml

from app.workflows.reviewerassignment import Reviewer, ReviewerAssignmentWorkflow


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
                {"login": "Alice", "available": True},
                {"login": "bob", "available": True},
            ]
        )
    )

    wf = ReviewerAssignmentWorkflow(mock_gh)

    await wf.handle_pr_opened(ctx, make_payload())

    reviewers = mock_gh.request_reviewers.call_args.args[3]

    assert reviewers == ["bob"]


def test_round_robin_prefers_reviewer_with_fewer_assignments():
    reviewers = [
        Reviewer(login="alice"),
        Reviewer(login="bob"),
    ]

    result = ReviewerAssignmentWorkflow._select_reviewers(
        reviewers,
        reviewers_count=1,
        strategy="round-robin",
        assignment_counts={"alice": 3, "bob": 1},
    )

    assert result == ["bob"]


def test_round_robin_rotates_by_assignment_count():
    reviewers = [
        Reviewer(login="alice"),
        Reviewer(login="bob"),
    ]

    assignment_counts = {}

    first = ReviewerAssignmentWorkflow._select_reviewers(
        reviewers,
        reviewers_count=1,
        strategy="round-robin",
        assignment_counts=assignment_counts,
    )
    assignment_counts[first[0]] = assignment_counts.get(first[0], 0) + 1

    second = ReviewerAssignmentWorkflow._select_reviewers(
        reviewers,
        reviewers_count=1,
        strategy="round-robin",
        assignment_counts=assignment_counts,
    )
    assignment_counts[second[0]] = assignment_counts.get(second[0], 0) + 1

    third = ReviewerAssignmentWorkflow._select_reviewers(
        reviewers,
        reviewers_count=1,
        strategy="round-robin",
        assignment_counts=assignment_counts,
    )

    assert first == ["alice"]
    assert second == ["bob"]
    assert third == ["alice"]


@pytest.mark.asyncio
async def test_get_assignment_counts_reads_reviewer_assignment_audits(db):
    from app.db.models import AuditLog

    db.add_all(
        [
            AuditLog(
                action="pr.reviewer_assigned",
                owner="hiero",
                repo="sdk-js",
                reason="Automatically assigned reviewers",
                metadata_json={"reviewers": ["alice", "bob"]},
            ),
            AuditLog(
                action="pr.reviewer_assigned",
                owner="hiero",
                repo="sdk-js",
                reason="Automatically assigned reviewers",
                metadata_json={"reviewers": ["Alice"]},
            ),
            AuditLog(
                action="pr.reviewer_assigned",
                owner="other-owner",
                repo="sdk-js",
                reason="Automatically assigned reviewers",
                metadata_json={"reviewers": ["charlie"]},
            ),
            AuditLog(
                action="pr.reviewed",
                owner="hiero",
                repo="sdk-js",
                reason="Review completed",
                metadata_json={"reviewers": ["charlie"]},
            ),
        ]
    )
    await db.commit()

    wf = ReviewerAssignmentWorkflow(AsyncMock())

    counts = await wf._get_assignment_counts(
        db,
        owner="hiero",
        repo="sdk-js",
    )

    assert counts == {
        "alice": 2,
        "bob": 1,
    }
