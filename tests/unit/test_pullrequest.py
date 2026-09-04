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

@pytest.mark.asyncio
async def test_opened_then_synchronize_updates_existing_quality_report(
    mock_gh,
    ctx,
):
    cfg = ctx["config"].workflows.pull_request

    cfg.ai_review.enabled = False
    cfg.reviewer_recommendation = False

    mock_gh.list_pr_files = AsyncMock(
        return_value=[
            {
                "filename": "app/example.py",
                "patch": "+print('test')",
            }
        ]
    )

    wf = PullRequestWorkflow(mock_gh)

    payload = make_payload()

    # First run: no existing Quality Gate comment.
    mock_gh.list_issue_comments = AsyncMock(return_value=[])

    await wf.handle_pr_opened(
        ctx,
        payload,
        "opened",
    )

    mock_gh.post_comment.assert_awaited_once()

    # Simulate the comment that was created on the first run.
    mock_gh.list_issue_comments.return_value = [
        {
            "id": 12345,
            "body": "## 🔍 Quality Gate Report\n\nPrevious report",
        }
    ]

    # Second run: synchronize.
    await wf.handle_pr_opened(
        ctx,
        payload,
        "synchronize",
    )

    # It must update the existing Quality Gate comment.
    mock_gh.update_comment.assert_awaited_once()

    updated_comment = mock_gh.update_comment.await_args
    assert updated_comment.args[2] == 12345
    assert updated_comment.args[3].startswith("## 🔍 Quality Gate Report")

    # It must NOT create another Quality Gate comment.
    assert mock_gh.post_comment.await_count == 1

@pytest.mark.asyncio
async def test_quality_report_ignores_unrelated_comment(mock_gh, ctx):
    cfg = ctx["config"].workflows.pull_request
    cfg.ai_review.enabled = False
    cfg.reviewer_recommendation = False

    mock_gh.list_pr_files = AsyncMock(
        return_value=[
            {
                "filename": "app/example.py",
                "patch": "+print('test')",
            }
        ]
    )

    mock_gh.list_issue_comments.return_value = [
        {
            "id": 999,
            "body": "Someone mentioned the Quality Gate Report here.",
        }
    ]

    wf = PullRequestWorkflow(mock_gh)

    await wf.handle_pr_opened(
        ctx,
        make_payload(),
        "synchronize",
    )

    mock_gh.update_comment.assert_not_awaited()
    mock_gh.post_comment.assert_awaited_once()

@pytest.mark.asyncio
async def test_require_tests_detects_test_file_from_second_page(mock_gh, ctx):
    cfg = ctx["config"].workflows.pull_request.quality_gates
    cfg.require_linked_issue = False
    cfg.require_tests = True
    cfg.require_dco = False
    cfg.require_gpg_signature = False
    cfg.max_files_changed = None
    cfg.allowed_branch_pattern = None
    cfg.require_changelog_entry = False

    first_page = [{"filename": f"src/file_{i}.py"} for i in range(100)]
    second_page = [{"filename": "tests/test_feature.py"}]

    mock_gh.list_pr_files = AsyncMock(
        return_value=first_page + second_page
    )

    wf = PullRequestWorkflow(mock_gh)

    checks = await wf._run_quality_checks(
        ctx,
        make_pr(),
    )

    tests_check = next(
        check for check in checks if check.name == "Tests"
    )

    assert tests_check.passed is True
    assert tests_check.detail == "Changes include test coverage ✅"


@pytest.mark.asyncio
async def test_require_changelog_detects_entry_from_second_page(mock_gh, ctx):
    cfg = ctx["config"].workflows.pull_request.quality_gates
    cfg.require_linked_issue = False
    cfg.require_tests = False
    cfg.require_dco = False
    cfg.require_gpg_signature = False
    cfg.max_files_changed = None
    cfg.allowed_branch_pattern = None
    cfg.require_changelog_entry = True

    first_page = [{"filename": f"src/file_{i}.py"} for i in range(100)]
    second_page = [{"filename": "CHANGELOG.md"}]

    mock_gh.list_pr_files = AsyncMock(
        return_value=first_page + second_page
    )

    wf = PullRequestWorkflow(mock_gh)

    checks = await wf._run_quality_checks(
        ctx,
        make_pr(),
    )

    changelog_check = next(
        check for check in checks if check.name == "Changelog"
    )

    assert changelog_check.passed is True
    assert changelog_check.detail == "CHANGELOG entry included ✅"
