# tests/unit/test_config_schema.py

import pytest
from pydantic import ValidationError

from app.config.schema import RepoConfig, find_unknown_keys

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
    assert cfg.workflows.onboarding.max_concurrent_assignments is None
    assert cfg.workflows.pull_request.stale_pr_days == 30
    assert cfg.workflows.issue_management.stale_issue_days == 60
    assert cfg.workflows.pr_health.enabled is True


def test_max_concurrent_assignments_validation():
    unlimited = RepoConfig.model_validate(
        {
            "repo": "hiero/x",
            "workflows": {
                "onboarding": {
                    "max_concurrent_assignments": None,
                }
            },
        }
    )
    assert unlimited.workflows.onboarding.max_concurrent_assignments is None

    limited = RepoConfig.model_validate(
        {
            "repo": "hiero/x",
            "workflows": {
                "onboarding": {
                    "max_concurrent_assignments": 5,
                }
            },
        }
    )
    assert limited.workflows.onboarding.max_concurrent_assignments == 5

    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "onboarding": {
                        "max_concurrent_assignments": 0,
                    }
                },
            }
        )


def test_ai_review_defaults():
    cfg = RepoConfig.model_validate(MINIMAL).workflows.pull_request.ai_review

    assert cfg.enabled is False
    assert cfg.model == "claude-sonnet-4-20250514"
    assert cfg.max_comments == 5
    assert cfg.focus_areas == ["security", "logic"]
    assert cfg.provider == "auto"
    assert cfg.max_retries == 2
    assert cfg.timeout_seconds == 60


@pytest.mark.parametrize("provider", ["auto", "anthropic", "openai", "ollama"])
def test_ai_review_provider_values(provider):
    cfg = RepoConfig.model_validate(
        {
            "repo": "hiero/x",
            "workflows": {
                "pull_request": {
                    "ai_review": {
                        "provider": provider,
                    }
                }
            },
        }
    )

    assert cfg.workflows.pull_request.ai_review.provider == provider


def test_ai_review_provider_rejects_unknown_value():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "pull_request": {
                        "ai_review": {
                            "provider": "unknown",
                        }
                    }
                },
            }
        )


@pytest.mark.parametrize("max_retries", [0, 1, 2, 5])
def test_ai_review_max_retries_accepts_valid_values(max_retries):
    cfg = RepoConfig.model_validate(
        {
            "repo": "hiero/x",
            "workflows": {
                "pull_request": {
                    "ai_review": {
                        "max_retries": max_retries,
                    }
                }
            },
        }
    )

    assert cfg.workflows.pull_request.ai_review.max_retries == max_retries


@pytest.mark.parametrize("max_retries", [-1, 6])
def test_ai_review_max_retries_rejects_out_of_range_values(max_retries):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "pull_request": {
                        "ai_review": {
                            "max_retries": max_retries,
                        }
                    }
                },
            }
        )


@pytest.mark.parametrize("timeout_seconds", [5, 60, 600])
def test_ai_review_timeout_accepts_valid_values(timeout_seconds):
    cfg = RepoConfig.model_validate(
        {
            "repo": "hiero/x",
            "workflows": {
                "pull_request": {
                    "ai_review": {
                        "timeout_seconds": timeout_seconds,
                    }
                }
            },
        }
    )

    assert cfg.workflows.pull_request.ai_review.timeout_seconds == timeout_seconds


@pytest.mark.parametrize("timeout_seconds", [4, 601])
def test_ai_review_timeout_rejects_out_of_range_values(timeout_seconds):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "pull_request": {
                        "ai_review": {
                            "timeout_seconds": timeout_seconds,
                        }
                    }
                },
            }
        )


def test_ai_review_max_comments_capped():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "pull_request": {"ai_review": {"max_comments": 999}}
                },
            }
        )


def test_invalid_ai_focus_area():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "pull_request": {
                        "ai_review": {"focus_areas": ["invalid"]}
                    }
                },
            }
        )


def test_stale_order_validator():
    """close_stale_after_days must be less than stale_issue_days."""
    with pytest.raises(ValidationError, match="close_stale_after_days"):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "issue_management": {
                        "stale_issue_days": 7,
                        "close_stale_after_days": 60,
                    }
                },
            }
        )


def test_mentor_strategy_validated():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/x",
                "workflows": {
                    "onboarding": {
                        "mentor_assignment_strategy": "magic"
                    }
                },
            }
        )


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
                "ai_review": {
                    "enabled": True,
                    "max_comments": 8,
                    "focus_areas": ["security", "tests"],
                },
                "quality_gates": {
                    "require_dco": True,
                    "require_tests": True,
                },
                "reviewer_recommendation": True,
            },
            "progression": {
                "requirements_for_junior_committer": {
                    "min_merged_prs": 3,
                    "min_reviews_given": 2,
                    "min_months_active": 1,
                    "require_endorsement_from": "committer",
                },
                "requirements_for_committer": {
                    "min_merged_prs": 15,
                    "min_reviews_given": 10,
                    "min_months_active": 6,
                    "require_endorsement_from": "maintainer",
                },
                "requirements_for_maintainer": {
                    "min_merged_prs": 50,
                    "min_reviews_given": 30,
                    "min_months_active": 12,
                    "require_endorsement_from": "maintainer",
                },
            },
            "issue_management": {
                "stale_issue_days": 90,
                "close_stale_after_days": 14,
                "label_escalation_rules": [
                    {
                        "label": "security",
                        "notify_team": "sec-team",
                        "after_hours": 24,
                    }
                ],
            },
        },
        "teams": {
            "maintainers": "maint",
            "committers": "comm",
            "junior_committers": "jc",
            "mentors": "mentors",
        },
    }
    cfg = RepoConfig.model_validate(data)
    assert cfg.workflows.pull_request.ai_review.max_comments == 8
    assert len(cfg.workflows.issue_management.label_escalation_rules) == 1


def test_issue_management_is_opt_in():
    cfg = RepoConfig.model_validate(MINIMAL)

    assert cfg.workflows.issue_management.enabled is False


def test_issue_management_can_be_enabled():
    cfg = RepoConfig.model_validate(
        {"repo": "hiero/sdk-js", "workflows": {"issue_management": {"enabled": True}}}
    )

    assert cfg.workflows.issue_management.enabled is True


def _health_weights(weights):
    return RepoConfig.model_validate(
        {"repo": "hiero/sdk-js", "workflows": {"pr_health": {"score_weights": weights}}}
    ).workflows.pr_health.score_weights


PERFECT_PR = {
    "has_tests": True,
    "has_linked_issue": True,
    "has_description": True,
    "dco_signed": True,
    "review_count": 5,
    "small_diff": True,
}


def test_partial_score_weights_are_scaled_so_a_perfect_pr_scores_100():
    from app.workflows.prhealth import PRHealthWorkflow

    weights = _health_weights({"has_tests": 0.5})

    assert PRHealthWorkflow._compute_score(PERFECT_PR, weights) == pytest.approx(100)


def test_unknown_score_weight_keys_are_dropped():
    weights = _health_weights({"has_tests": 0.5, "has_test": 9.0})

    assert weights == {"has_tests": 1.0}


def test_default_score_weights_are_unchanged():
    cfg = RepoConfig.model_validate(MINIMAL)

    assert sum(cfg.workflows.pr_health.score_weights.values()) == pytest.approx(1.0)
    assert cfg.workflows.pr_health.score_weights["has_tests"] == 0.25


@pytest.mark.parametrize("weights", [{"has_tests": -1.0}, {"has_test": 1.0}, {}])
def test_unusable_score_weights_are_rejected(weights):
    with pytest.raises(ValidationError):
        _health_weights(weights)


@pytest.mark.parametrize("pattern", ["[", "a" * 201])
def test_unusable_branch_pattern_is_rejected(pattern):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({
            "repo": "hiero/sdk-js",
            "workflows": {"pull_request": {"quality_gates": {"allowed_branch_pattern": pattern}}},
        })


def test_valid_branch_pattern_is_accepted():
    cfg = RepoConfig.model_validate({
        "repo": "hiero/sdk-js",
        "workflows": {"pull_request": {"quality_gates": {"allowed_branch_pattern": "^(feat|fix)/"}}},
    })

    assert cfg.workflows.pull_request.quality_gates.allowed_branch_pattern == "^(feat|fix)/"


def test_find_unknown_keys_reports_typos_at_every_level():
    data = {
        "repo": "hiero/sdk-js",
        "workflow": {},
        "workflows": {
            "pull_request": {"quality_gate": {"require_dco": False}},
            "issue_management": {
                "label_escalation_rules": [{"label": "x", "notify_team": "t", "oops": 1}]
            },
        },
    }

    assert sorted(find_unknown_keys(data, RepoConfig)) == [
        "workflow",
        "workflows.issue_management.label_escalation_rules[0].oops",
        "workflows.pull_request.quality_gate",
    ]


def test_find_unknown_keys_is_empty_for_a_valid_config():
    assert find_unknown_keys(MINIMAL, RepoConfig) == []


@pytest.mark.parametrize("path", ["templates/hiero-bot.yml", ".github/hiero-bot.yml"])
def test_shipped_configs_use_only_known_keys(path):
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    data = yaml.safe_load((root / path).read_text(encoding="utf-8"))

    assert find_unknown_keys(data, RepoConfig) == []
