
# app/config/schema.py
# Pydantic v2 config schema & validation

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.logger import get_logger
from app.utils.safe_regex import validate_pattern


log = get_logger("config.schema")


# -------------------------------------------------------------------
# Literal Types
# -------------------------------------------------------------------

FocusArea = Literal["security", "performance", "style", "logic", "tests"]

AIProvider = Literal["auto", "anthropic", "openai", "ollama"]

MentorStrategy = Literal["round-robin", "least-busy", "expertise-match"]

ReviewerAssignmentStrategy = Literal["round-robin", "random"]

ReviewerNotifyComment = Literal["off", "mention"]

Verdict = Literal["approve", "request_changes", "comment"]


# -------------------------------------------------------------------
# Role Requirements
# -------------------------------------------------------------------

class RoleRequirements(BaseModel):
    min_merged_prs: int = Field(ge=0)
    min_reviews_given: int = Field(ge=0)
    min_months_active: int = Field(ge=0)


# -------------------------------------------------------------------
# Onboarding
# -------------------------------------------------------------------

class OnboardingConfig(BaseModel):
    enabled: bool = True

    check_human_contributors: bool = True

    require_signed_cla: bool = False

    cla_signatures_file: str = ".github/cla-signatures.json"

    cla_document_url: str | None = None

    welcome_first_time_only: bool = True

    minimum_account_age_days: int = Field(default=0, ge=0)

    minimum_public_contributions: int = Field(default=0, ge=0)

    max_concurrent_assignments: int | None = Field(
        default=None,
        ge=1,
    )

    auto_assign_mentor: bool = False

    mentor_assignment_strategy: MentorStrategy = "round-robin"

    welcome_message: str | None = None

    onboarding_checklist: list[str] = Field(default_factory=list)


# -------------------------------------------------------------------
# Pull Request - AI Review
# -------------------------------------------------------------------

class AIReviewConfig(BaseModel):
    enabled: bool = False

    model: str = "claude-sonnet-4-20250514"

    max_comments: int = Field(default=5, ge=1, le=20)

    focus_areas: list[FocusArea] = Field(
        default_factory=lambda: ["security", "logic"]
    )

    provider: AIProvider = "auto"

    max_retries: int = Field(default=2, ge=0, le=5)

    timeout_seconds: int = Field(default=60, ge=5, le=600)


# -------------------------------------------------------------------
# Quality Gates
# -------------------------------------------------------------------

class QualityGatesConfig(BaseModel):
    require_tests: bool = True

    require_dco: bool = True

    require_gpg_signature: bool = False

    max_files_changed: int | None = Field(default=None, gt=0)

    require_changelog_entry: bool = False

    require_linked_issue: bool = False

    allowed_branch_pattern: str | None = None

    @field_validator("allowed_branch_pattern")
    @classmethod
    def _branch_pattern_must_be_usable(
        cls,
        pattern: str | None,
    ) -> str | None:
        if pattern is None:
            return None

        return validate_pattern(pattern)


# -------------------------------------------------------------------
# Pull Request Configuration
# -------------------------------------------------------------------

class PullRequestConfig(BaseModel):
    enabled: bool = True

    ai_review: AIReviewConfig = Field(
        default_factory=AIReviewConfig
    )

    quality_gates: QualityGatesConfig = Field(
        default_factory=QualityGatesConfig
    )

    auto_label: bool = True

    reviewer_recommendation: bool = True


# -------------------------------------------------------------------
# Progression
# -------------------------------------------------------------------

class ProgressionConfig(BaseModel):
    enabled: bool = True

    recommend_issues_after_merge: bool = True

    recommendation_count: int = Field(default=3, ge=1, le=10)

    requirements_for_junior_committer: RoleRequirements = Field(
        default_factory=lambda: RoleRequirements(
            min_merged_prs=3,
            min_reviews_given=2,
            min_months_active=1,
        )
    )

    requirements_for_committer: RoleRequirements = Field(
        default_factory=lambda: RoleRequirements(
            min_merged_prs=15,
            min_reviews_given=10,
            min_months_active=6,
        )
    )

    requirements_for_maintainer: RoleRequirements = Field(
        default_factory=lambda: RoleRequirements(
            min_merged_prs=50,
            min_reviews_given=30,
            min_months_active=12,
        )
    )

    celebrate_milestones: bool = True


# -------------------------------------------------------------------
# Issue Management
# -------------------------------------------------------------------

class LabelEscalationRule(BaseModel):
    label: str

    notify_team: str


class IssueManagementConfig(BaseModel):
    enabled: bool = False

    stale_issue_days: int = Field(default=60, gt=0)

    close_stale_after_days: int = Field(default=7, gt=0)

    stale_label: str = "stale"

    exempt_labels: list[str] = Field(
        default_factory=lambda: ["pinned", "security", "in-progress"]
    )

    auto_unassign_inactive_days: int = Field(default=14, gt=0)

    label_escalation_rules: list[LabelEscalationRule] = Field(
        default_factory=list
    )


# -------------------------------------------------------------------
# PR Health
# -------------------------------------------------------------------

HEALTH_SIGNALS = frozenset({
    "has_tests",
    "has_linked_issue",
    "has_description",
    "dco_signed",
    "review_count",
    "small_diff",
})


class PRHealthConfig(BaseModel):
    enabled: bool = True

    score_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "has_tests": 0.25,
            "has_linked_issue": 0.15,
            "has_description": 0.15,
            "dco_signed": 0.20,
            "review_count": 0.15,
            "small_diff": 0.10,
        }
    )

    comment_threshold: int = Field(default=60, ge=0, le=100)

    label_healthy_above: int = Field(default=75, ge=0, le=100)

    @field_validator("score_weights")
    @classmethod
    def _normalise_score_weights(
        cls,
        weights: dict[str, float],
    ) -> dict[str, float]:

        unknown = sorted(set(weights) - HEALTH_SIGNALS)

        if unknown:
            log.warning(
                "Ignoring unknown pr_health.score_weights keys: %s",
                ", ".join(unknown),
            )

        known = {
            name: weight
            for name, weight in weights.items()
            if name in HEALTH_SIGNALS
        }

        if any(weight < 0 for weight in known.values()):
            raise ValueError(
                "score_weights must not be negative"
            )

        total = sum(known.values())

        if total <= 0:
            raise ValueError(
                "score_weights needs at least one known signal "
                "with a positive weight"
            )

        return {
            name: weight / total
            for name, weight in known.items()
        }


# -------------------------------------------------------------------
# Reviewer Assignment
# -------------------------------------------------------------------

class ReviewerAssignmentConfig(BaseModel):
    enabled: bool = False

    availability_file: str = ".github/reviewers.yml"

    reviewers_count: int = Field(default=1, ge=1)

    strategy: ReviewerAssignmentStrategy = "round-robin"

    exclude_pr_author: bool = True

    fallback_to_all_if_none_available: bool = True

    notify_comment: ReviewerNotifyComment = "off"


# -------------------------------------------------------------------
# Teams and Labels
# -------------------------------------------------------------------

class TeamsConfig(BaseModel):
    mentors: str = "mentors"


class DifficultyLabels(BaseModel):
    intermediate: str = "intermediate"


# -------------------------------------------------------------------
# Root Workflow Configuration
# -------------------------------------------------------------------

class WorkflowsConfig(BaseModel):
    onboarding: OnboardingConfig = Field(
        default_factory=OnboardingConfig
    )

    pull_request: PullRequestConfig = Field(
        default_factory=PullRequestConfig
    )

    progression: ProgressionConfig = Field(
        default_factory=ProgressionConfig
    )

    issue_management: IssueManagementConfig = Field(
        default_factory=IssueManagementConfig
    )

    pr_health: PRHealthConfig = Field(
        default_factory=PRHealthConfig
    )

    reviewer_assignment: ReviewerAssignmentConfig = Field(
        default_factory=ReviewerAssignmentConfig
    )


class RepoConfig(BaseModel):
    repo: str = Field(
        pattern=r"^[a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+$"
    )

    workflows: WorkflowsConfig = Field(
        default_factory=WorkflowsConfig
    )

    difficulty_labels: DifficultyLabels = Field(
        default_factory=DifficultyLabels
    )

    teams: TeamsConfig = Field(
        default_factory=TeamsConfig
    )

    @model_validator(mode="after")
    def validate_stale_order(self) -> RepoConfig:
        issue_management = self.workflows.issue_management

        if (
            issue_management.close_stale_after_days
            >= issue_management.stale_issue_days
        ):
            raise ValueError(
                "close_stale_after_days must be less than "
                "stale_issue_days"
            )

        return self


# -------------------------------------------------------------------
# Unknown-Key Detection
# -------------------------------------------------------------------

def _model_types(annotation: object) -> list[type[BaseModel]]:
    """
    Return every Pydantic model mentioned in a type annotation.

    Supports Optional, Union, lists and other generic annotations.
    """

    if (
        isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
    ):
        return [annotation]

    found: list[type[BaseModel]] = []

    for arg in get_args(annotation):
        found.extend(_model_types(arg))

    return found


def find_unknown_keys(
    data: object,
    model: type[BaseModel],
    prefix: str = "",
) -> list[str]:
    """
    Find unknown keys in a configuration dictionary.

    Pydantic normally ignores unknown keys. This function detects
    misspelled configuration keys, including nested configuration.
    """

    unknown: list[str] = []

    if not isinstance(data, dict):
        return unknown

    for key, value in data.items():

        path = f"{prefix}{key}"

        field = model.model_fields.get(key)

        if field is None:
            unknown.append(path)
            continue

        nested_models = _model_types(field.annotation)

        if not nested_models:
            continue

        nested_model = nested_models[0]

        if isinstance(value, dict):

            unknown.extend(
                find_unknown_keys(
                    value,
                    nested_model,
                    f"{path}.",
                )
            )

        elif isinstance(value, list):

            for index, item in enumerate(value):

                unknown.extend(
                    find_unknown_keys(
                        item,
                        nested_model,
                        f"{path}[{index}].",
                    )
                )

    return unknown
