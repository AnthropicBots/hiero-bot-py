# app/config/schema.py — Pydantic v2 config schema & validation

from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


RoleLevel = Literal["contributor", "junior-committer", "committer", "maintainer"]
FocusArea = Literal["security", "performance", "style", "logic", "tests"]
MentorStrategy = Literal["round-robin", "least-busy", "expertise-match"]
Verdict = Literal["approve", "request_changes", "comment"]


# ── Role requirements ──────────────────────────────────────────

class RoleRequirements(BaseModel):
    min_merged_prs: int = Field(ge=0)
    min_reviews_given: int = Field(ge=0)
    min_months_active: int = Field(ge=0)
    require_endorsement_from: RoleLevel


# ── Onboarding ────────────────────────────────────────────────

class OnboardingConfig(BaseModel):
    enabled: bool = True
    check_human_contributors: bool = True
    require_signed_cla: bool = False
    minimum_account_age_days: int = Field(default=0, ge=0)
    minimum_public_contributions: int = Field(default=0, ge=0)
    auto_assign_mentor: bool = False
    mentor_assignment_strategy: MentorStrategy = "round-robin"
    welcome_message: Optional[str] = None
    onboarding_checklist: list[str] = []


# ── Pull Request ──────────────────────────────────────────────

class AIReviewConfig(BaseModel):
    enabled: bool = False
    model: str = "claude-sonnet-4-20250514"
    max_comments: int = Field(default=5, ge=1, le=20)
    focus_areas: list[FocusArea] = ["security", "logic"]


class QualityGatesConfig(BaseModel):
    require_tests: bool = True
    require_dco: bool = True
    require_gpg_signature: bool = False
    min_reviewers: int = Field(default=1, ge=0)
    max_files_changed: Optional[int] = Field(default=None, gt=0)
    require_changelog_entry: bool = False
    require_linked_issue: bool = False
    allowed_branch_pattern: Optional[str] = None


class PullRequestConfig(BaseModel):
    enabled: bool = True
    ai_review: AIReviewConfig = Field(default_factory=AIReviewConfig)
    quality_gates: QualityGatesConfig = Field(default_factory=QualityGatesConfig)
    auto_label: bool = True
    stale_pr_days: int = Field(default=30, gt=0)
    auto_close_stale: bool = False
    reviewer_recommendation: bool = True  # NEW


# ── Progression ───────────────────────────────────────────────

class ProgressionConfig(BaseModel):
    enabled: bool = True
    recommend_issues_after_merge: bool = True
    recommendation_count: int = Field(default=3, ge=1, le=10)
    requirements_for_junior_committer: RoleRequirements = Field(
        default_factory=lambda: RoleRequirements(
            min_merged_prs=3, min_reviews_given=2, min_months_active=1,
            require_endorsement_from="committer"
        )
    )
    requirements_for_committer: RoleRequirements = Field(
        default_factory=lambda: RoleRequirements(
            min_merged_prs=15, min_reviews_given=10, min_months_active=6,
            require_endorsement_from="maintainer"
        )
    )
    requirements_for_maintainer: RoleRequirements = Field(
        default_factory=lambda: RoleRequirements(
            min_merged_prs=50, min_reviews_given=30, min_months_active=12,
            require_endorsement_from="maintainer"
        )
    )
    celebrate_milestones: bool = True


# ── Issue Management ──────────────────────────────────────────

class LabelEscalationRule(BaseModel):
    label: str
    notify_team: str
    after_hours: int = Field(gt=0)


class IssueManagementConfig(BaseModel):
    enabled: bool = True
    stale_issue_days: int = Field(default=60, gt=0)
    close_stale_after_days: int = Field(default=7, gt=0)
    stale_label: str = "stale"
    exempt_labels: list[str] = ["pinned", "security", "in-progress"]
    auto_unassign_inactive_days: int = Field(default=14, gt=0)
    label_escalation_rules: list[LabelEscalationRule] = []
    create_good_first_issues: bool = False


# ── PR Health ─────────────────────────────────────────────────

class PRHealthConfig(BaseModel):          # NEW workflow
    enabled: bool = True
    score_weights: dict[str, float] = {
        "has_tests": 0.25,
        "has_linked_issue": 0.15,
        "has_description": 0.15,
        "dco_signed": 0.20,
        "review_count": 0.15,
        "small_diff": 0.10,
    }
    comment_threshold: int = Field(default=60, ge=0, le=100)   # only comment if score < threshold
    label_healthy_above: int = Field(default=75, ge=0, le=100)


# ── Teams & Labels ────────────────────────────────────────────

class TeamsConfig(BaseModel):
    maintainers: str = "maintainers"
    committers: str = "committers"
    junior_committers: str = "junior-committers"
    mentors: str = "mentors"


class DifficultyLabels(BaseModel):
    good_first_issue: str = "good first issue"
    intermediate: str = "intermediate"
    advanced: str = "advanced"


# ── Root ──────────────────────────────────────────────────────

class WorkflowsConfig(BaseModel):
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    pull_request: PullRequestConfig = Field(default_factory=PullRequestConfig)
    progression: ProgressionConfig = Field(default_factory=ProgressionConfig)
    issue_management: IssueManagementConfig = Field(default_factory=IssueManagementConfig)
    pr_health: PRHealthConfig = Field(default_factory=PRHealthConfig)


class RepoConfig(BaseModel):
    repo: str = Field(pattern=r"^[a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+$")
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)
    difficulty_labels: DifficultyLabels = Field(default_factory=DifficultyLabels)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)

    @model_validator(mode="after")
    def validate_stale_order(self) -> "RepoConfig":
        im = self.workflows.issue_management
        if im.close_stale_after_days >= im.stale_issue_days:
            raise ValueError("close_stale_after_days must be less than stale_issue_days")
        return self
