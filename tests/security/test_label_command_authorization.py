from unittest.mock import AsyncMock

import pytest

from app.github.webhooks import WebhookRouter


def make_router(permission: str):
    gh = AsyncMock()
    gh.get_collaborator_permission = AsyncMock(return_value=permission)
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

    gh.add_label.assert_awaited_once_with("hiero", "sdk-js", 7, "bug", 42)
    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_non_author_collaborator_can_label():
    router, gh = make_router(permission="maintain")
    payload = issue_payload(issue_author="alice", commenter="bob")

    await router._handle_label_command("triage", payload, CTX)

    gh.add_label.assert_awaited_once_with("hiero", "sdk-js", 7, "triage", 42)


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