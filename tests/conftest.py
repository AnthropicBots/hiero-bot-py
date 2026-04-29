# tests/conftest.py

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.db.database import Base
from app.config.schema import RepoConfig


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def mock_gh():
    gh = AsyncMock()
    gh.post_comment = AsyncMock()
    gh.add_label = AsyncMock()
    gh.add_assignees = AsyncMock()
    gh.remove_assignees = AsyncMock()
    gh.close_issue = AsyncMock()
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pr_files = AsyncMock(return_value=[])
    gh.list_pr_commits = AsyncMock(return_value=[])
    gh.list_pr_reviews = AsyncMock(return_value=[])
    gh.get_combined_status = AsyncMock(return_value={"statuses": []})
    gh.get_user = AsyncMock(return_value={
        "login": "alice",
        "type": "User",
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 10,
    })
    gh.list_team_members = AsyncMock(return_value=[{"login": "mentor1"}])
    gh.get = AsyncMock(return_value=[])
    return gh


@pytest.fixture
def base_config():
    return RepoConfig.model_validate({
        "repo": "hiero/sdk-js",
        "workflows": {
            "onboarding": {
                "enabled": True,
                "check_human_contributors": True,
                "minimum_account_age_days": 0,
                "minimum_public_contributions": 0,
                "auto_assign_mentor": False,
                "mentor_assignment_strategy": "round-robin",
            },
            "pull_request": {"enabled": True},
            "progression": {
                "enabled": True,
                "recommend_issues_after_merge": True,
                "recommendation_count": 3,
                "requirements_for_junior_committer": {
                    "min_merged_prs": 3, "min_reviews_given": 2,
                    "min_months_active": 1, "require_endorsement_from": "committer"
                },
                "requirements_for_committer": {
                    "min_merged_prs": 15, "min_reviews_given": 10,
                    "min_months_active": 6, "require_endorsement_from": "maintainer"
                },
                "requirements_for_maintainer": {
                    "min_merged_prs": 50, "min_reviews_given": 30,
                    "min_months_active": 12, "require_endorsement_from": "maintainer"
                },
                "celebrate_milestones": True,
            },
            "issue_management": {
                "enabled": True,
                "stale_issue_days": 60,
                "close_stale_after_days": 7,
                "stale_label": "stale",
                "exempt_labels": ["pinned", "security"],
                "auto_unassign_inactive_days": 14,
                "label_escalation_rules": [],
            },
            "pr_health": {"enabled": True},
        }
    })


@pytest.fixture
def ctx(base_config, db):
    return {
        "owner": "hiero",
        "repo": "sdk-js",
        "installation_id": 42,
        "config": base_config,
        "db": db,
    }
