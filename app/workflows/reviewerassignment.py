from __future__ import annotations

import base64
import random

import yaml
from pydantic import BaseModel, ValidationError

from app.config.schema import ReviewerAssignmentStrategy
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.reviewerassignment")


class Reviewer(BaseModel):
    login: str
    available: bool = True


class ReviewersFile(BaseModel):
    reviewers: list[Reviewer]


class ReviewerAssignmentWorkflow:
    """Automatically assign reviewers to newly opened pull requests."""

    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    async def _load_reviewers(
        self,
        owner: str,
        repo: str,
        installation_id: int,
        path: str,
    ) -> list[Reviewer]:
        """Load reviewers from the repository availability file."""
        raw_b64 = await self._gh.get_file_content(
            owner,
            repo,
            path,
            installation_id,
        )

        if raw_b64 is None:
            log.warning("Reviewer availability file %s not found", path)
            return []

        try:
            content = base64.b64decode(raw_b64).decode("utf-8")
            data = yaml.safe_load(content) or {}
            reviewers = ReviewersFile.model_validate(data)
            return reviewers.reviewers
        except (ValidationError, yaml.YAMLError) as exc:
            log.warning(
                "Failed to load reviewer availability file %s: %s",
                path,
                exc,
            )
            return []

    @staticmethod
    def _select_reviewers(
        reviewers: list[Reviewer],
        *,
        pr_number: int,
        reviewers_count: int,
        strategy: ReviewerAssignmentStrategy,
    ) -> list[str]:
        """Select reviewers using the configured strategy."""
        if not reviewers:
            return []

        reviewers_count = min(reviewers_count, len(reviewers))

        if strategy == "random":
            return [
                reviewer.login
                for reviewer in random.sample(reviewers, reviewers_count)
            ]

        start = pr_number % len(reviewers)

        ordered = reviewers[start:] + reviewers[:start]

        return [
            reviewer.login
            for reviewer in ordered[:reviewers_count]
        ]

    async def handle_pr_opened(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.reviewer_assignment
        if not cfg.enabled:
            return

        pr = payload.get("pull_request", {})
        if not pr:
            return

        owner = ctx["owner"]
        repo = ctx["repo"]
        installation_id = ctx["installation_id"]
        db = ctx["db"]

        pr_number = pr["number"]
        author = pr["user"]["login"]

        reviewers = await self._load_reviewers(
            owner,
            repo,
            installation_id,
            cfg.availability_file,
        )

        if not reviewers:
            log.info("No reviewers available for assignment")
            return

        available_reviewers = [
            reviewer
            for reviewer in reviewers
            if reviewer.available
        ]

        if cfg.exclude_pr_author:
            available_reviewers = [
                reviewer
                for reviewer in available_reviewers
                if reviewer.login != author
            ]

        if (
            not available_reviewers
            and cfg.fallback_to_all_if_none_available
        ):
            available_reviewers = reviewers

            if cfg.exclude_pr_author:
                available_reviewers = [
                    reviewer
                    for reviewer in available_reviewers
                    if reviewer.login != author
                ]

        if not available_reviewers:
            log.info(
                "No eligible reviewers found for PR #%s",
                pr_number,
            )
            return

        selected_reviewers = self._select_reviewers(
            available_reviewers,
            pr_number=pr_number,
            reviewers_count=cfg.reviewers_count,
            strategy=cfg.strategy,
        )

        if not selected_reviewers:
            return

        await self._gh.request_reviewers(
            owner,
            repo,
            pr_number,
            selected_reviewers,
            installation_id,
        )

        if cfg.notify_comment == "mention":
            mentions = " ".join(
                f"@{reviewer}"
                for reviewer in selected_reviewers
            )

            await self._gh.post_comment(
                owner,
                repo,
                pr_number,
                f"{mentions} please review this PR 🙏",
                installation_id,
            )

        await audit.record(
            db,
            action="pr.reviewer_assigned",
            owner=owner,
            repo=repo,
            target_number=pr_number,
            target_login=author,
            reason="Automatically assigned reviewers",
            metadata={
                "reviewers": selected_reviewers,
                "reviewers_count": len(selected_reviewers),
                "strategy": cfg.strategy,
            },
        )

        await db.commit()