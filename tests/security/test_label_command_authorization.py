from unittest.mock import AsyncMock

import pytest

from app.github.webhooks import WebhookRouter


def make_router(permission: str, repo_labels: list[dict] | None = None):
    gh = AsyncMock()
    gh.get_collaborator_permission = AsyncMock(return_value=permission)
    gh.list_labels = AsyncMock(
        return_value=repo_labels
        if repo_labels is not None
        else [
            {"name": "bug"},
            {"name": "Bug-Fix"},
            {"name": "triage"},
            {"name": "action: merge"},
            {"name": "action: review"},
            {"name": "pinned"},
            {"name": "security"},
            {"name": "in-progress"},
        ]
    )
    gh.add_label = AsyncMock()
    gh.post_comment = AsyncMock()
    config_loader = AsyncMock()
    return WebhookRouter(gh, config_loader), gh


CTX = {"owner": "hiero", "repo": "sdk-js", "installation_id": 42}


def issue_payload(issue_author: str, commenter: str, issue_number: int = 7):
    return {
        "issue": {"number": issue_number, "user": {"login": issue_author}},
        "comment": {"user": {"login": commenter}},
    }


@pytest.mark.asyncio
async def test_issue_author_without_write_access_cannot_self_label():
    router, gh = make_router(permission="none")
    payload = issue_payload(issue_author="mallory", commenter="mallory")

    await router._handle_label_command("pinned", payload, CTX)

    gh.get_collaborator_permission.assert_awaited_once_with(
        "hiero", "sdk-js", "mallory", 42
    )
    gh.add_label.assert_not_called()
    gh.post_comment.assert_awaited_once()
    assert "only committers and maintainers" in gh.post_comment.await_args.args[3]


@pytest.mark.asyncio
async def test_issue_author_with_write_access_can_label():
    router, gh = make_router(permission="write")
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("bug", payload, CTX)

    gh.add_label.assert_awaited_once_with(
        "hiero", "sdk-js", 7, "bug", 42, create_if_missing=False
    )
    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_non_author_collaborator_can_label():
    router, gh = make_router(permission="maintain")
    payload = issue_payload(issue_author="alice", commenter="bob")

    await router._handle_label_command("triage", payload, CTX)

    gh.add_label.assert_awaited_once_with(
        "hiero", "sdk-js", 7, "triage", 42, create_if_missing=False
    )


@pytest.mark.asyncio
async def test_non_author_non_collaborator_is_rejected():
    router, gh = make_router(permission="read")
    payload = issue_payload(issue_author="alice", commenter="random-contributor")

    await router._handle_label_command("security", payload, CTX)

    gh.add_label.assert_not_called()
    gh.post_comment.assert_awaited_once()


@pytest.mark.parametrize("exempt_label", ["pinned", "security", "in-progress"])
@pytest.mark.asyncio
async def test_stale_exempt_labels_cannot_be_self_applied_by_author(exempt_label):
    router, gh = make_router(permission="none")
    payload = issue_payload(issue_author="mallory", commenter="mallory")

    await router._handle_label_command(exempt_label, payload, CTX)

    gh.add_label.assert_not_called()


@pytest.mark.asyncio
async def test_label_unknown_label_not_created_or_applied_comment_posted():
    router, gh = make_router(permission="write", repo_labels=[{"name": "Bug-Fix"}])
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("nonexistent-label", payload, CTX)

    gh.add_label.assert_not_called()
    gh.post_comment.assert_awaited_once()
    assert "nonexistent-label" in gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_label_multi_word_name():
    router, gh = make_router(permission="write")
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("action: merge", payload, CTX)

    gh.add_label.assert_awaited_once_with(
        "hiero", "sdk-js", 7, "action: merge", 42, create_if_missing=False
    )
    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_label_quoted_multi_word_name():
    router, gh = make_router(permission="write")
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command('"action: merge"', payload, CTX)

    gh.add_label.assert_awaited_once_with(
        "hiero", "sdk-js", 7, "action: merge", 42, create_if_missing=False
    )
    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_label_comma_separated_labels():
    router, gh = make_router(permission="write")
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("action: review, Bug-Fix", payload, CTX)

    assert gh.add_label.await_count == 2
    gh.add_label.assert_any_await(
        "hiero", "sdk-js", 7, "action: review", 42, create_if_missing=False
    )
    gh.add_label.assert_any_await(
        "hiero", "sdk-js", 7, "Bug-Fix", 42, create_if_missing=False
    )
    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_label_case_insensitive_matching():
    router, gh = make_router(permission="write")
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("BUG-fix", payload, CTX)

    gh.add_label.assert_awaited_once_with(
        "hiero", "sdk-js", 7, "Bug-Fix", 42, create_if_missing=False
    )
    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_label_mixed_valid_and_unknown_labels():
    router, gh = make_router(permission="write")
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("Bug-Fix, unknown-xyz", payload, CTX)

    gh.add_label.assert_awaited_once_with(
        "hiero", "sdk-js", 7, "Bug-Fix", 42, create_if_missing=False
    )
    gh.post_comment.assert_awaited_once()
    assert "unknown-xyz" in gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_label_close_match_suggestion():
    router, gh = make_router(permission="write", repo_labels=[{"name": "Bug-Fix"}])
    payload = issue_payload(issue_author="alice", commenter="alice")

    await router._handle_label_command("bugfix", payload, CTX)

    gh.add_label.assert_not_called()
    gh.post_comment.assert_awaited_once()
    comment = gh.post_comment.call_args[0][3]
    assert "bugfix" in comment and "Bug-Fix" in comment