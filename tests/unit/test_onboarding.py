# tests/unit/test_onboarding.py

import pytest
from unittest.mock import AsyncMock
from app.workflows.onboarding import OnboardingWorkflow


def make_payload(login="alice", issue_number=1, sender_type="User", assignees=None):
    return {
        "sender": {"login": login, "type": sender_type},
        "issue": {"number": issue_number, "user": {"login": login},
                  "assignees": assignees or []},
        "comment": {"user": {"login": login}, "body": "/assign"},
    }


@pytest.mark.asyncio
async def test_welcomes_first_time_contributor(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value=[])
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_awaited_once()
    assert "Welcome to Hiero" in mock_gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_skips_bot_sender(mock_gh, ctx):
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload(sender_type="Bot"))
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_dependabot_login(mock_gh, ctx):
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload(login="dependabot[bot]"))
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_existing_contributor(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value=[{"login": "alice"}])
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.onboarding.enabled = False
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_assigns_mentor_when_enabled(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value=[])
    ctx["config"].workflows.onboarding.auto_assign_mentor = True
    ctx["config"].teams.mentors = "mentors"
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.add_assignees.assert_awaited()
    assert "mentor1" in mock_gh.add_assignees.call_args[0][3]


@pytest.mark.asyncio
async def test_self_assign_success(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value=[])
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload(assignees=[]))
    mock_gh.add_assignees.assert_awaited_once()
    assert mock_gh.add_assignees.call_args[0][3] == ["alice"]


@pytest.mark.asyncio
async def test_self_assign_already_assigned(mock_gh, ctx):
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload(assignees=[{"login": "alice"}]))
    mock_gh.add_assignees.assert_not_awaited()
    assert "already assigned" in mock_gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_self_assign_blocked_by_min_age(mock_gh, ctx):
    ctx["config"].workflows.onboarding.minimum_account_age_days = 90
    mock_gh.get_user = AsyncMock(return_value={
        "login": "newbie", "type": "User",
        "created_at": "2026-04-25T00:00:00Z", "public_repos": 5,
    })
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload(login="newbie", assignees=[]))
    mock_gh.add_assignees.assert_not_awaited()
    assert "90 days old" in mock_gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_welcome_includes_checklist(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value=[])
    ctx["config"].workflows.onboarding.onboarding_checklist = ["Read CONTRIBUTING.md", "Sign DCO"]
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    body = mock_gh.post_comment.call_args[0][3]
    assert "Read CONTRIBUTING.md" in body
    assert "Sign DCO" in body


@pytest.mark.asyncio
async def test_welcome_includes_custom_message(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value=[])
    ctx["config"].workflows.onboarding.welcome_message = "Extra special welcome!"
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    assert "Extra special welcome!" in mock_gh.post_comment.call_args[0][3]
