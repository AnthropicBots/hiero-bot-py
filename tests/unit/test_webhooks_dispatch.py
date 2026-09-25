# tests/unit/test_webhooks_dispatch.py
#
# Issue #96 — app/github/webhooks.py had only partial coverage: the
# timestamp-skew replay defense, the event dispatcher, the slash-command
# handlers, and several installation-event edge cases had no tests at all.

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.db.models import Account
from app.github.webhooks import WebhookRouter


def make_request(headers: dict, json_body: dict, body: bytes = b""):
    request = MagicMock()
    request.headers = headers
    request.body = AsyncMock(return_value=body)
    request.json = AsyncMock(return_value=json_body)
    return request


_UNSET = object()


def make_router(config=_UNSET):
    gh = MagicMock()
    config_loader = MagicMock()
    config_loader.invalidate = MagicMock()
    config_loader.load = AsyncMock(
        return_value={"enabled": True} if config is _UNSET else config
    )
    router = WebhookRouter(gh, config_loader)
    router._verify_signature = MagicMock()  # signature checked elsewhere
    return router, gh, config_loader


# ── Timestamp skew (defense-in-depth #2) ────────────────────────────────

@pytest.mark.asyncio
async def test_stale_date_header_is_rejected(db):
    router, _, _ = make_router()
    stale = datetime.now(timezone.utc) - timedelta(seconds=999)
    request = make_request(
        {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "skew-test-1",
            "Date": stale.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        },
        {},
    )

    with pytest.raises(HTTPException) as exc:
        await router.handle(request, db)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_fresh_date_header_is_accepted(db):
    router, _, _ = make_router()
    now = datetime.now(timezone.utc)
    request = make_request(
        {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "skew-test-2",
            "Date": now.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        },
        {},
    )

    result = await router.handle(request, db)
    assert result == {"ok": True, "skipped": "no repo/installation"}


@pytest.mark.asyncio
async def test_unparseable_date_header_does_not_fail_the_request(db):
    router, _, _ = make_router()
    request = make_request(
        {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "skew-test-3",
            "Date": "not-a-real-date",
        },
        {},
    )

    result = await router.handle(request, db)
    assert result == {"ok": True, "skipped": "no repo/installation"}


# ── Top-level routing via handle() ──────────────────────────────────────

@pytest.mark.asyncio
async def test_installation_event_is_routed_before_repo_check(db):
    router, _, _ = make_router()
    router._handle_installation = AsyncMock(return_value={"ok": True, "action": "created"})

    request = make_request(
        {"X-GitHub-Event": "installation"},
        {"action": "created", "installation": {"id": 1}},
    )

    result = await router.handle(request, db)
    assert result == {"ok": True, "action": "created"}
    router._handle_installation.assert_awaited_once()


@pytest.mark.asyncio
async def test_installation_repositories_event_is_routed_before_repo_check(db):
    router, _, _ = make_router()
    router._handle_installation_repositories = AsyncMock(
        return_value={"ok": True, "action": "added"}
    )

    request = make_request(
        {"X-GitHub-Event": "installation_repositories"},
        {"action": "added", "installation": {"id": 1}},
    )

    result = await router.handle(request, db)
    assert result == {"ok": True, "action": "added"}
    router._handle_installation_repositories.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_config_is_skipped(db):
    router, _, _config_loader = make_router(config=None)

    payload = {
        "repository": {"owner": {"login": "hiero"}, "name": "sdk-js"},
        "installation": {"id": 42},
    }
    request = make_request({"X-GitHub-Event": "ping"}, payload)

    result = await router.handle(request, db)
    assert result == {"ok": True, "skipped": "no config"}


# ── _dispatch(): issues / pull_request / issue_comment routing ─────────

@pytest.mark.asyncio
async def test_dispatch_issues_opened_calls_onboarding(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.OnboardingWorkflow") as mock_cls:
        mock_cls.return_value.handle_new_contributor = AsyncMock()
        await router._dispatch("issues", {"action": "opened"}, ctx)
        mock_cls.return_value.handle_new_contributor.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_issues_labeled_calls_issue_management(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.IssueManagementWorkflow") as mock_cls:
        mock_cls.return_value.handle_label_escalation = AsyncMock()
        await router._dispatch("issues", {"action": "labeled"}, ctx)
        mock_cls.return_value.handle_label_escalation.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_pull_request_draft_is_skipped(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.PullRequestWorkflow") as mock_cls:
        mock_cls.return_value.handle_pr_opened = AsyncMock()
        await router._dispatch(
            "pull_request",
            {"action": "opened", "pull_request": {"draft": True}},
            ctx,
        )
        mock_cls.return_value.handle_pr_opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_pull_request_ready_for_review_calls_pr_reviewer_and_health(ctx):
    router, _gh, _ = make_router()

    with (
        patch("app.github.webhooks.PullRequestWorkflow") as mock_pr,
        patch("app.github.webhooks.ReviewerAssignmentWorkflow") as mock_reviewer,
        patch("app.github.webhooks.PRHealthWorkflow") as mock_health,
    ):
        mock_pr.return_value.handle_pr_opened = AsyncMock()
        mock_reviewer.return_value.handle_pr_opened = AsyncMock()
        mock_health.return_value.score_pr = AsyncMock()

        await router._dispatch(
            "pull_request",
            {
                "action": "ready_for_review",
                "pull_request": {"draft": False},
            },
            ctx,
        )

        mock_pr.return_value.handle_pr_opened.assert_awaited_once()
        mock_reviewer.return_value.handle_pr_opened.assert_awaited_once()
        mock_health.return_value.score_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_pull_request_opened_calls_pr_reviewer_and_health(ctx):
    router, _gh, _ = make_router()

    with (
        patch("app.github.webhooks.PullRequestWorkflow") as mock_pr,
        patch("app.github.webhooks.ReviewerAssignmentWorkflow") as mock_reviewer,
        patch("app.github.webhooks.PRHealthWorkflow") as mock_health,
    ):
        mock_pr.return_value.handle_pr_opened = AsyncMock()
        mock_reviewer.return_value.handle_pr_opened = AsyncMock()
        mock_health.return_value.score_pr = AsyncMock()

        await router._dispatch(
            "pull_request",
            {"action": "opened", "pull_request": {"draft": False}},
            ctx,
        )

        mock_pr.return_value.handle_pr_opened.assert_awaited_once()
        mock_reviewer.return_value.handle_pr_opened.assert_awaited_once()
        mock_health.return_value.score_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_pull_request_synchronize_skips_reviewer_assignment(ctx):
    """Reviewer assignment only runs on the initial 'opened' action."""
    router, _gh, _ = make_router()

    with (
        patch("app.github.webhooks.PullRequestWorkflow") as mock_pr,
        patch("app.github.webhooks.ReviewerAssignmentWorkflow") as mock_reviewer,
        patch("app.github.webhooks.PRHealthWorkflow") as mock_health,
    ):
        mock_pr.return_value.handle_pr_opened = AsyncMock()
        mock_reviewer.return_value.handle_pr_opened = AsyncMock()
        mock_health.return_value.score_pr = AsyncMock()

        await router._dispatch(
            "pull_request",
            {"action": "synchronize", "pull_request": {"draft": False}},
            ctx,
        )

        mock_pr.return_value.handle_pr_opened.assert_awaited_once()
        mock_reviewer.return_value.handle_pr_opened.assert_not_awaited()
        mock_health.return_value.score_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_pull_request_closed_merged_calls_progression(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.ProgressionWorkflow") as mock_cls:
        mock_cls.return_value.handle_merged_pr = AsyncMock()
        await router._dispatch(
            "pull_request",
            {"action": "closed", "pull_request": {"draft": False, "merged": True}},
            ctx,
        )
        mock_cls.return_value.handle_merged_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_pull_request_closed_not_merged_skips_progression(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.ProgressionWorkflow") as mock_cls:
        mock_cls.return_value.handle_merged_pr = AsyncMock()
        await router._dispatch(
            "pull_request",
            {"action": "closed", "pull_request": {"draft": False, "merged": False}},
            ctx,
        )
        mock_cls.return_value.handle_merged_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_issue_comment_with_slash_prefix_triggers_command(ctx):
    router, _gh, _ = make_router()
    router._handle_slash_command = AsyncMock()

    await router._dispatch(
        "issue_comment",
        {"action": "created", "comment": {"body": "/help"}},
        ctx,
    )
    router._handle_slash_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_issue_comment_without_slash_prefix_is_ignored(ctx):
    router, _gh, _ = make_router()
    router._handle_slash_command = AsyncMock()

    await router._dispatch(
        "issue_comment",
        {"action": "created", "comment": {"body": "just a normal comment"}},
        ctx,
    )
    router._handle_slash_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_unknown_event_does_not_raise(ctx):
    router, _gh, _ = make_router()
    await router._dispatch("star", {"action": "created"}, ctx)  # no handler — should just log


@pytest.mark.asyncio
async def test_dispatch_swallows_handler_exceptions(ctx):
    """A bug in one workflow must not crash the webhook delivery."""
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.OnboardingWorkflow") as mock_cls:
        mock_cls.return_value.handle_new_contributor = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        # Should not raise.
        await router._dispatch("issues", {"action": "opened"}, ctx)


# ── Slash commands ───────────────────────────────────────────────────────

def slash_payload(body: str, issue_number=5, commenter="alice"):
    return {
        "comment": {"body": body, "user": {"login": commenter}},
        "issue": {"number": issue_number},
    }


@pytest.mark.asyncio
async def test_slash_assign_calls_onboarding_self_assign(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.OnboardingWorkflow") as mock_cls:
        mock_cls.return_value.handle_self_assign = AsyncMock()
        await router._handle_slash_command("/assign", slash_payload("/assign"), ctx)
        mock_cls.return_value.handle_self_assign.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_unassign_removes_and_comments(ctx):
    router, gh, _ = make_router()
    gh.remove_assignees = AsyncMock()
    gh.post_comment = AsyncMock()

    await router._handle_slash_command(
        "/unassign", slash_payload("/unassign", issue_number=7, commenter="bob"), ctx
    )

    gh.remove_assignees.assert_awaited_once_with(
        ctx["owner"], ctx["repo"], 7, ["bob"], ctx["installation_id"]
    )
    gh.post_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_unassign_without_issue_number_is_a_noop(ctx):
    router, gh, _ = make_router()
    gh.remove_assignees = AsyncMock()

    payload = {"comment": {"body": "/unassign", "user": {"login": "bob"}}, "issue": {}}
    await router._handle_slash_command("/unassign", payload, ctx)

    gh.remove_assignees.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_check_eligibility_calls_progression(ctx):
    router, _gh, _ = make_router()

    with patch("app.github.webhooks.ProgressionWorkflow") as mock_cls:
        mock_cls.return_value.check_and_report = AsyncMock()
        await router._handle_slash_command(
            "/check-eligibility", slash_payload("/check-eligibility"), ctx
        )
        mock_cls.return_value.check_and_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_help_posts_help_text(ctx):
    router, gh, _ = make_router()
    gh.post_comment = AsyncMock()

    await router._handle_slash_command("/help", slash_payload("/help", issue_number=9), ctx)

    gh.post_comment.assert_awaited_once()
    assert "Hiero Bot Help" in gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_slash_help_without_issue_number_is_a_noop(ctx):
    router, gh, _ = make_router()
    gh.post_comment = AsyncMock()

    payload = {"comment": {"body": "/help", "user": {"login": "bob"}}, "issue": {}}
    await router._handle_slash_command("/help", payload, ctx)

    gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_label_dispatches_to_label_handler(ctx):
    router, _gh, _ = make_router()
    router._handle_label_command = AsyncMock()

    await router._handle_slash_command(
        "/label good-first-issue",
        slash_payload("/label good-first-issue"),
        ctx,
    )
    router._handle_label_command.assert_awaited_once()
    assert router._handle_label_command.call_args[0][0] == "good-first-issue"


@pytest.mark.asyncio
async def test_slash_label_without_args_is_a_noop(ctx):
    router, _gh, _ = make_router()
    router._handle_label_command = AsyncMock()

    await router._handle_slash_command("/label", slash_payload("/label"), ctx)
    router._handle_label_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_slash_command_is_ignored(ctx):
    router, gh, _ = make_router()
    gh.post_comment = AsyncMock()

    await router._handle_slash_command("/nonsense", slash_payload("/nonsense"), ctx)
    gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_command_without_issue_number_returns_early(ctx):
    router, gh, _ = make_router()
    gh.get_collaborator_permission = AsyncMock()

    payload = {"comment": {"body": "/label bug", "user": {"login": "bob"}}, "issue": {}}
    await router._handle_label_command("bug", payload, ctx)

    gh.get_collaborator_permission.assert_not_awaited()


# ── Installation event edge cases ───────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_installation_without_id_is_skipped(db):
    router, _, _ = make_router()
    result = await router._handle_installation({"action": "created", "installation": {}}, db)
    assert result == {"ok": True, "skipped": "no installation id"}


@pytest.mark.asyncio
async def test_handle_installation_unsuspended_updates_existing_account(db):
    router, _, _ = make_router()

    acc = Account(github_installation_id=5555, org_login="oldname", plan_tier="free")
    db.add(acc)
    await db.commit()

    payload = {
        "action": "unsuspended",
        "installation": {
            "id": 5555,
            "account": {"id": 111, "login": "newname", "type": "Organization"},
        },
    }
    result = await router._handle_installation(payload, db)
    assert result == {"ok": True, "action": "unsuspended"}

    from sqlalchemy import select
    row = (
        await db.execute(select(Account).where(Account.github_installation_id == 5555))
    ).scalar_one()
    assert row.org_login == "newname"
    assert row.suspended_at is None


@pytest.mark.asyncio
async def test_handle_installation_repositories_without_id_is_skipped(db):
    router, _, _ = make_router()
    result = await router._handle_installation_repositories(
        {"action": "added", "installation": {}}, db
    )
    assert result == {"ok": True, "skipped": "no installation id"}


@pytest.mark.asyncio
async def test_handle_installation_repositories_unknown_account_is_skipped(db):
    router, _, _ = make_router()
    result = await router._handle_installation_repositories(
        {"action": "added", "installation": {"id": 424242}, "repositories_added": []}, db
    )
    assert result == {"ok": True, "skipped": "account not found"}
