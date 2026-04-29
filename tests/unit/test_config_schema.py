# tests/unit/test_config_schema.py

import pytest
from pydantic import ValidationError
from app.config.schema import RepoConfig


MINIMAL = {"repo": "hiero/sdk-js", "workflows": {}}


def test_minimal_config_valid():
    cfg = RepoConfig.model_validate(MINIMAL)
    assert cfg.repo == "hiero/sdk-js"


def test_invalid_repo_format():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({"repo": "not-valid", "workflows": {}})


def test_defaults_applied():
    cfg = RepoConfig.model_validate(MINIMAL)
    assert cfg.workflows.onboarding.enabled is True
    assert cfg.workflows.onboarding.minimum_account_age_days == 0
    assert cfg.workflows.pull_request.stale_pr_days == 30
    assert cfg.workflows.issue_management.stale_issue_days == 60
    assert cfg.workflows.pr_health.enabled is True


def test_ai_review_max_comments_capped():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({
            "repo": "hiero/x",
            "workflows": {"pull_request": {"ai_review": {"max_comments": 999}}}
        })


def test_invalid_ai_focus_area():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({
            "repo": "hiero/x",
            "workflows": {"pull_request": {"ai_review": {"focus_areas": ["invalid"]}}}
        })


def test_stale_order_validator():
    """close_stale_after_days must be less than stale_issue_days."""
    with pytest.raises(ValidationError, match="close_stale_after_days"):
        RepoConfig.model_validate({
            "repo": "hiero/x",
            "workflows": {
                "issue_management": {
                    "stale_issue_days": 7,
                    "close_stale_after_days": 60,
                }
            }
        })


def test_mentor_strategy_validated():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({
            "repo": "hiero/x",
            "workflows": {"onboarding": {"mentor_assignment_strategy": "magic"}}
        })


def test_full_valid_config():
    data = {
        "repo": "hiero/sdk-ts",
        "workflows": {
            "onboarding": {
                "enabled": True,
                "minimum_account_age_days": 30,
                "auto_assign_mentor": True,
                "mentor_assignment_strategy": "round-robin",
                "onboarding_checklist": ["Read CONTRIBUTING.md"],
            },
            "pull_request": {
                "enabled": True,
                "ai_review": {"enabled": True, "max_comments": 8, "focus_areas": ["security", "tests"]},
                "quality_gates": {"require_dco": True, "require_tests": True},
                "reviewer_recommendation": True,
            },
            "progression": {
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
            },
            "issue_management": {
                "stale_issue_days": 90,
                "close_stale_after_days": 14,
                "label_escalation_rules": [
                    {"label": "security", "notify_team": "sec-team", "after_hours": 24}
                ],
            },
        },
        "teams": {"maintainers": "maint", "committers": "comm",
                  "junior_committers": "jc", "mentors": "mentors"},
    }
    cfg = RepoConfig.model_validate(data)
    assert cfg.workflows.pull_request.ai_review.max_comments == 8
    assert len(cfg.workflows.issue_management.label_escalation_rules) == 1
