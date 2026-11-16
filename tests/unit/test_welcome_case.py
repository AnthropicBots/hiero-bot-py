
import pytest

from app.utils import audit
from app.workflows.onboarding import OnboardingWorkflow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_login", "queried_login", "expected"),
    [
        ("Alice", "alice", True),
        ("alice", "ALICE", True),
        ("ALICE", "Alice", True),
        ("Alice", "Alice", True),
        ("Alice", "bob", False),
    ],
)
async def test_already_welcomed_matches_login_case_insensitively(
    ctx, stored_login, queried_login, expected
):
    await audit.record(
        ctx["db"],
        action="contributor.welcomed",
        owner="hiero",
        repo="sdk-js",
        target_number=99,
        target_login=stored_login,
        reason="Earlier welcome",
    )
    await ctx["db"].commit()

    result = await OnboardingWorkflow._already_welcomed(
        OnboardingWorkflow.__new__(OnboardingWorkflow),
        ctx,
        queried_login,
    )

    assert result is expected
