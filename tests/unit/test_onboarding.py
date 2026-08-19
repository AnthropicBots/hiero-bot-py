# tests/unit/test_onboarding.py

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.utils import audit
from app.workflows.onboarding import OnboardingWorkflow, looks_like_bot


def make_payload(
    login="alice",
    issue_number=1,
    sender_type="User",
    assignees=None,
    author_association="NONE",
):
    return {
        "sender": {"login": login, "type": sender_type},
        "issue": {
            "number": issue_number,
            "user": {"login": login},
            "assignees": assignees or [],
            "author_association": author_association,
        },
        "comment": {"user": {"login": login}, "body": "/assign"},
    }


def encode(document):
    return base64.b64encode(json.dumps(document).encode()).decode()


def only_own_issue(issue_number=1):
    """GitHub issue-history response containing just the issue being handled."""
    return AsyncMock(return_value=[{"number": issue_number}])


# ── Welcome flow ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_welcomes_first_time_contributor(mock_gh, ctx):
    mock_gh.get = only_own_issue()
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_awaited_once()
    assert "Welcome to Hiero" in mock_gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_skips_contributor_with_earlier_issues(mock_gh, ctx):
    """Regression for #40 — every issue-opener used to get a welcome."""
    mock_gh.get = AsyncMock(return_value=[{"number": 1}, {"number": 7}])
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload(issue_number=1))
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_established_author_association(mock_gh, ctx):
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(
        ctx, make_payload(author_association="COLLABORATOR")
    )
    mock_gh.post_comment.assert_not_awaited()
    mock_gh.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_contributor_welcomed_before(mock_gh, ctx, db):
    await audit.record(
        db,
        action="contributor.welcomed",
        owner="hiero",
        repo="sdk-js",
        target_login="alice",
        target_number=99,
        reason="Earlier welcome",
    )
    await db.commit()

    mock_gh.get = only_own_issue()
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_welcomes_everyone_when_first_time_only_disabled(mock_gh, ctx):
    ctx["config"].workflows.onboarding.welcome_first_time_only = False
    mock_gh.get = AsyncMock(return_value=[{"number": 1}, {"number": 7}])
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_when_issue_history_lookup_fails(mock_gh, ctx):
    mock_gh.get = AsyncMock(side_effect=RuntimeError("api down"))
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_not_awaited()


# ── Bot detection (#42: check_human_contributors) ─────────────


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
async def test_bot_type_still_skipped_when_human_check_disabled(mock_gh, ctx):
    ctx["config"].workflows.onboarding.check_human_contributors = False
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload(sender_type="Bot"))
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_heuristic_bot_match_off_when_human_check_disabled(mock_gh, ctx):
    """`check_human_contributors: false` must actually change behaviour (#42)."""
    ctx["config"].workflows.onboarding.check_human_contributors = False
    mock_gh.get = only_own_issue()
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload(login="renovate"))
    mock_gh.post_comment.assert_awaited_once()


@pytest.mark.parametrize(
    "login",
    ["dependabot[bot]", "renovate", "github-actions", "MERGIFY", "snyk-bot"],
)
def test_known_bot_logins_detected(login):
    assert looks_like_bot(login)


@pytest.mark.parametrize("login", ["talbot", "abbot", "robotics-sam", "botany-dev"])
def test_human_logins_containing_bot_are_not_bots(login):
    assert not looks_like_bot(login)


def test_account_type_bot_wins_over_login():
    assert looks_like_bot("alice", account_type="Bot")


# ── Config / mentors ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_disabled(mock_gh, ctx):
    ctx["config"].workflows.onboarding.enabled = False
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_assigns_mentor_when_enabled(mock_gh, ctx):
    mock_gh.get = only_own_issue()
    ctx["config"].workflows.onboarding.auto_assign_mentor = True
    ctx["config"].teams.mentors = "mentors"
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    mock_gh.add_assignees.assert_awaited()
    assert "mentor1" in mock_gh.add_assignees.call_args[0][3]


# ── Self-assignment ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_assign_success(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload(assignees=[]))
    mock_gh.add_assignees.assert_awaited_once()
    assert mock_gh.add_assignees.call_args[0][3] == ["alice"]


@pytest.mark.asyncio
async def test_self_assign_already_assigned(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": [{"login": "alice"}]})
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload(assignees=[{"login": "alice"}]))
    mock_gh.add_assignees.assert_not_awaited()
    assert "already assigned" in mock_gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_self_assign_blocked_by_min_age(mock_gh, ctx):
    ctx["config"].workflows.onboarding.minimum_account_age_days = 90

    # Create a dynamic date that is exactly 10 days ago so this test never expires
    recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_user = AsyncMock(
        return_value={
            "login": "newbie",
            "type": "User",
            "created_at": recent_date,
            "public_repos": 5,
        }
    )
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload(login="newbie", assignees=[]))
    mock_gh.add_assignees.assert_not_awaited()
    assert "90 days old" in mock_gh.post_comment.call_args[0][3]


@pytest.mark.asyncio
async def test_account_check_failure_does_not_block_assignment(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_user = AsyncMock(side_effect=RuntimeError("api down"))
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())
    mock_gh.add_assignees.assert_awaited_once()


# ── CLA gate (#42: require_signed_cla) ────────────────────────


@pytest.mark.asyncio
async def test_cla_gate_blocks_unsigned_contributor(mock_gh, ctx):
    """Regression for #42 — require_signed_cla used to be inert."""
    ctx["config"].workflows.onboarding.require_signed_cla = True
    ctx["config"].workflows.onboarding.cla_document_url = "https://example.org/cla"
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_file_content = AsyncMock(
        return_value=encode({"signedContributors": [{"name": "bob"}]})
    )

    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())

    mock_gh.add_assignees.assert_not_awaited()
    body = mock_gh.post_comment.call_args[0][3]
    assert "Contributor License Agreement" in body
    assert "https://example.org/cla" in body


@pytest.mark.asyncio
async def test_cla_gate_allows_signed_contributor(mock_gh, ctx):
    ctx["config"].workflows.onboarding.require_signed_cla = True
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_file_content = AsyncMock(
        return_value=encode({"signedContributors": [{"name": "Alice"}]})
    )

    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())

    mock_gh.add_assignees.assert_awaited_once()


@pytest.mark.asyncio
async def test_cla_gate_fails_closed_when_file_missing(mock_gh, ctx):
    ctx["config"].workflows.onboarding.require_signed_cla = True
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_file_content = AsyncMock(return_value=None)

    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())

    mock_gh.add_assignees.assert_not_awaited()


@pytest.mark.asyncio
async def test_cla_gate_fails_closed_when_file_lookup_fails(mock_gh, ctx):
    ctx["config"].workflows.onboarding.require_signed_cla = True
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_file_content = AsyncMock(
        side_effect=RuntimeError("GitHub API down")
    )

    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())

    mock_gh.add_assignees.assert_not_awaited()


@pytest.mark.asyncio
async def test_cla_gate_fails_closed_on_invalid_json(mock_gh, ctx):
    ctx["config"].workflows.onboarding.require_signed_cla = True
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    mock_gh.get_file_content = AsyncMock(
        return_value=base64.b64encode(b"not json at all").decode()
    )

    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())

    mock_gh.add_assignees.assert_not_awaited()


@pytest.mark.asyncio
async def test_cla_not_checked_when_not_required(mock_gh, ctx):
    mock_gh.get = AsyncMock(return_value={"number": 1, "assignees": []})
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_self_assign(ctx, make_payload())
    mock_gh.get_file_content.assert_not_awaited()
    mock_gh.add_assignees.assert_awaited_once()


@pytest.mark.parametrize(
    "document",
    [
        ["alice", "bob"],
        [{"login": "alice"}],
        [{"username": "alice"}],
        {"signatures": [{"name": "alice"}]},
        {"signedContributors": [{"name": "alice"}]},
    ],
)
def test_signatory_formats_supported(document):
    assert "alice" in OnboardingWorkflow._signatory_logins(document)


def test_signatory_extraction_ignores_unknown_shapes():
    assert OnboardingWorkflow._signatory_logins({"totally": "different"}) == set()


# ── Welcome body ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_welcome_includes_checklist(mock_gh, ctx):
    mock_gh.get = only_own_issue()
    ctx["config"].workflows.onboarding.onboarding_checklist = [
        "Read CONTRIBUTING.md",
        "Sign DCO",
    ]
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    body = mock_gh.post_comment.call_args[0][3]
    assert "Read CONTRIBUTING.md" in body
    assert "Sign DCO" in body


@pytest.mark.asyncio
async def test_welcome_includes_custom_message(mock_gh, ctx):
    mock_gh.get = only_own_issue()
    ctx["config"].workflows.onboarding.welcome_message = "Extra special welcome!"
    wf = OnboardingWorkflow(mock_gh)
    await wf.handle_new_contributor(ctx, make_payload())
    assert "Extra special welcome!" in mock_gh.post_comment.call_args[0][3]
