# tests/unit/test_config_schema.py

import ast
import pathlib
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

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


def test_close_stale_period_can_exceed_stale_threshold():
    """The close period is additional time after an issue becomes stale."""
    cfg = RepoConfig.model_validate(
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
    assert cfg.workflows.issue_management.close_stale_after_days == 60


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
                },
                "requirements_for_committer": {
                    "min_merged_prs": 15,
                    "min_reviews_given": 10,
                    "min_months_active": 6,
                },
                "requirements_for_maintainer": {
                    "min_merged_prs": 50,
                    "min_reviews_given": 30,
                    "min_months_active": 12,
                },
            },
            "issue_management": {
                "stale_issue_days": 90,
                "close_stale_after_days": 14,
                "label_escalation_rules": [
                    {
                        "label": "security",
                        "notify_team": "sec-team",
                    }
                ],
            },
        },
        "teams": {
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


def test_every_schema_field_has_a_runtime_reader():
    """Check schema field paths against configuration-derived runtime reads."""
    root = pathlib.Path(__file__).resolve().parents[2]
    app_root = root / "app"
    schema_path = app_root / "config" / "schema.py"
    runtime_paths = set()

    def config_path(node, aliases):
        """Return a dotted path only when an expression derives from config."""
        if isinstance(node, ast.Name):
            return aliases.get(node.id)

        if isinstance(node, ast.Subscript) and (
            isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "config"
        ):
            return ""

        if isinstance(node, ast.Attribute):
            parent = config_path(node.value, aliases)
            if parent is not None:
                return f"{parent}.{node.attr}" if parent else node.attr

        return None

    for path in app_root.rglob("*.py"):
        if path == schema_path:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"))

        scopes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        for scope in scopes:
            aliases = {}

            for _ in range(len(scope.body) + 1):
                changed = False

                for node in ast.walk(scope):
                    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue

                    value = node.value
                    if value is None:
                        continue

                    resolved = config_path(value, aliases)
                    if resolved is None:
                        continue

                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )

                    for target in targets:
                        if not isinstance(target, ast.Name):
                            continue

                        existing = aliases.get(target.id)

                        if existing is None:
                            aliases[target.id] = resolved
                            changed = True
                        elif existing != resolved:
                            continue

                if not changed:
                    break

            for node in ast.walk(scope):
                resolved = config_path(node, aliases)
                if resolved:
                    runtime_paths.add(resolved)

    def nested_models(annotation):
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return {annotation}

        models = set()
        for argument in get_args(annotation):
            models.update(nested_models(argument))
        return models

    def collect_field_paths(model, prefix="", seen=None):
        if seen is None:
            seen = set()

        if model in seen:
            return set()

        seen = {*seen, model}
        paths = set()

        for name, field in model.model_fields.items():
            field_path = f"{prefix}.{name}" if prefix else name
            paths.add(field_path)

            for nested_model in nested_models(field.annotation):
                paths.update(collect_field_paths(nested_model, field_path, seen))

        return paths

    schema_paths = collect_field_paths(RepoConfig)

    indirect_reader_contracts = {
        "workflows.issue_management.close_stale_after_days": [
            ("app/workflows/issuemanagement.py", "IssueManagementWorkflow", "_scan_issue", "cfg.close_stale_after_days"),
        ],
        "workflows.issue_management.exempt_labels": [
            ("app/workflows/issuemanagement.py", "IssueManagementWorkflow", "_scan_issue", "cfg.exempt_labels"),
        ],
        "workflows.issue_management.stale_label": [
            ("app/workflows/issuemanagement.py", "IssueManagementWorkflow", "_scan_issue", "cfg.stale_label"),
        ],
        "workflows.issue_management.label_escalation_rules.label": [
            ("app/workflows/issuemanagement.py", "IssueManagementWorkflow", "handle_label_escalation", "r.label"),
        ],
        "workflows.issue_management.label_escalation_rules.notify_team": [
            ("app/workflows/issuemanagement.py", "IssueManagementWorkflow", "handle_label_escalation", "rule.notify_team"),
        ],
        "workflows.onboarding.cla_document_url": [
            ("app/workflows/onboarding.py", "OnboardingWorkflow", "_cla_notice", "cfg.cla_document_url"),
        ],
        "workflows.onboarding.onboarding_checklist": [
            ("app/workflows/onboarding.py", "OnboardingWorkflow", "_build_welcome", "cfg.onboarding_checklist"),
        ],
        "workflows.onboarding.welcome_message": [
            ("app/workflows/onboarding.py", "OnboardingWorkflow", "_build_welcome", "cfg.welcome_message"),
        ],
        "workflows.progression.requirements_for_junior_committer": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "cfg.requirements_for_junior_committer"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "cfg.requirements_for_junior_committer"),
        ],
        "workflows.progression.requirements_for_committer": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "cfg.requirements_for_committer"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "cfg.requirements_for_committer"),
        ],
        "workflows.progression.requirements_for_maintainer": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "cfg.requirements_for_maintainer"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "cfg.requirements_for_maintainer"),
        ],
        "workflows.progression.requirements_for_junior_committer.min_merged_prs": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_merged_prs"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_merged_prs"),
        ],
        "workflows.progression.requirements_for_junior_committer.min_reviews_given": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_reviews_given"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_reviews_given"),
        ],
        "workflows.progression.requirements_for_junior_committer.min_months_active": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_months_active"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_months_active"),
        ],
        "workflows.progression.requirements_for_committer.min_merged_prs": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_merged_prs"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_merged_prs"),
        ],
        "workflows.progression.requirements_for_committer.min_reviews_given": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_reviews_given"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_reviews_given"),
        ],
        "workflows.progression.requirements_for_committer.min_months_active": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_months_active"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_months_active"),
        ],
        "workflows.progression.requirements_for_maintainer.min_merged_prs": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_merged_prs"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_merged_prs"),
        ],
        "workflows.progression.requirements_for_maintainer.min_reviews_given": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_reviews_given"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_reviews_given"),
        ],
        "workflows.progression.requirements_for_maintainer.min_months_active": [
            ("app/workflows/progression.py", "ProgressionWorkflow", "_check_eligibility", "reqs.min_months_active"),
            ("app/workflows/progression.py", "ProgressionWorkflow", "_build_full_report", "reqs.min_months_active"),
        ],
        "workflows.pull_request.ai_review.focus_areas": [
            ("app/ai/reviewer.py", "AIReviewer", "_build_prompt", "cfg.focus_areas"),
        ],
        "workflows.pull_request.ai_review.max_retries": [
            ("app/ai/reviewer.py", "AIReviewer", "_complete_with_retries", 'getattr(cfg, "max_retries", 2)'),
        ],
        "workflows.pull_request.ai_review.model": [
            ("app/ai/reviewer.py", "AIReviewer", "review", "cfg.model"),
        ],
        "workflows.pull_request.ai_review.provider": [
            ("app/ai/reviewer.py", "AIReviewer", "_get_backend", 'getattr(cfg, "provider", "auto")'),
        ],
        "workflows.pull_request.ai_review.timeout_seconds": [
            ("app/ai/reviewer.py", "AIReviewer", "review", 'getattr(cfg, "timeout_seconds", 60)'),
        ],
    }

    schema_only_fields = {"repo"}
    source_trees = {}

    def find_method(tree, class_name, method_name):
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != class_name:
                continue

            for child in node.body:
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == method_name
                ):
                    return child

        return None

    def has_expression(function, expression):
        expected = ast.dump(ast.parse(expression, mode="eval").body)

        return any(
            ast.dump(node) == expected
            for node in ast.walk(function)
        )

    def has_role_requirements(function, role, method_name):
        requirements_attribute = f"requirements_for_{role.replace('-', '_')}"

        for node in ast.walk(function):
            if method_name == "_check_eligibility":
                if not isinstance(node, ast.Tuple):
                    continue

                elements = node.elts
                if (
                    len(elements) == 2
                    and isinstance(elements[0], ast.Constant)
                    and elements[0].value == role
                    and ast.dump(elements[1])
                    == ast.dump(
                        ast.parse(
                            f"cfg.{requirements_attribute}",
                            mode="eval",
                        ).body
                    )
                ):
                    return True

            elif method_name == "_build_full_report":
                if not isinstance(node, ast.Call):
                    continue

                if not isinstance(node.func, ast.Name) or node.func.id != "row":
                    continue

                if len(node.args) < 2:
                    continue

                if (
                    isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == role
                    and ast.dump(node.args[1])
                    == ast.dump(
                        ast.parse(
                            f"cfg.{requirements_attribute}",
                            mode="eval",
                        ).body
                    )
                ):
                    return True

        return False

    for field_path, contracts in indirect_reader_contracts.items():
        for source_file, class_name, method_name, expression in contracts:
            if source_file not in source_trees:
                source_path = root / source_file
                source_trees[source_file] = ast.parse(
                    source_path.read_text(encoding="utf-8")
                )

            function = find_method(
                source_trees[source_file],
                class_name,
                method_name,
            )

            assert function is not None, (
                f"Reader method {class_name}.{method_name} for "
                f"{field_path!r} was not found in {source_file!r}"
            )
            assert has_expression(function, expression), (
                f"Expected {expression!r} in "
                f"{class_name}.{method_name} for {field_path!r}"
            )


    progression_tree = source_trees.get("app/workflows/progression.py")
    assert progression_tree is not None

    for role in ("junior-committer", "committer", "maintainer"):
        for method_name in ("_check_eligibility", "_build_full_report"):
            function = find_method(
                progression_tree,
                "ProgressionWorkflow",
                method_name,
            )

            assert function is not None
            assert has_role_requirements(function, role, method_name), (
                f"{method_name} does not associate {role!r} with "
                f"cfg.requirements_for_{role.replace('-', '_')}"
            )

    for path in tuple(runtime_paths):
        parts = path.split(".")
        runtime_paths.update(
            ".".join(parts[:index])
            for index in range(1, len(parts))
        )

    unused = sorted(
        schema_paths
        - runtime_paths
        - set(indirect_reader_contracts)
        - schema_only_fields
    )

    assert unused == [], (
        "Schema fields without detected configuration readers: "
        f"{unused}"
    )
