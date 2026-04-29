# app/workflows/prhealth.py — PR health scoring (new workflow)

from __future__ import annotations
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.github.client import GitHubClient
from app.db.models import PRHealthScore
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.prhealth")

LABEL_HEALTHY = "health: 💚 healthy"
LABEL_NEEDS_WORK = "health: 🔧 needs work"


class PRHealthWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    async def score_pr(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.pr_health
        if not cfg.enabled:
            return

        pr = payload.get("pull_request", {})
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        pr_number = pr["number"]
        author = pr["user"]["login"]

        # Gather signals
        files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
        reviews = await self._gh.list_pr_reviews(owner, repo, pr_number, inst)
        sha = pr.get("head", {}).get("sha", "")
        status_data = await self._gh.get_combined_status(owner, repo, sha, inst) if sha else {}

        signals = self._compute_signals(pr, files, reviews, status_data)
        score = self._compute_score(signals, cfg.score_weights)

        label = LABEL_HEALTHY if score >= cfg.label_healthy_above else LABEL_NEEDS_WORK

        # Persist to DB
        record = PRHealthScore(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr_author=author,
            score=round(score, 1),
            has_tests=signals["has_tests"],
            has_linked_issue=signals["has_linked_issue"],
            has_description=signals["has_description"],
            dco_signed=signals["dco_signed"],
            review_count=signals["review_count"],
            files_changed=len(files),
            label_applied=label,
        )
        db.add(record)

        # Label the PR
        await self._gh.add_label(owner, repo, pr_number, label, inst)

        # Comment if score is below threshold
        if score < cfg.comment_threshold:
            await self._gh.post_comment(
                owner, repo, pr_number,
                self._build_health_comment(score, signals, cfg.label_healthy_above),
                inst,
            )

        await audit.record(
            db, action="pr.health_scored", owner=owner, repo=repo,
            target_number=pr_number, target_login=author,
            reason=f"PR health score: {score:.0f}/100",
            metadata={"score": round(score, 1), "signals": signals},
        )
        await db.commit()
        log.info("PR #%d health score: %.0f/100 (%s)", pr_number, score, label)

    # ── Signal extraction ─────────────────────────────────────

    @staticmethod
    def _compute_signals(
        pr: dict, files: list[dict], reviews: list[dict], status: dict
    ) -> dict:
        body = pr.get("body") or ""
        test_re = [re.compile(p) for p in
                   [r"\.test\.[jt]sx?$", r"\.spec\.[jt]sx?$",
                    r"tests?/", r"test_.*\.py$", r".*_test\.py$"]]

        statuses = status.get("statuses", [])
        dco_ok = any(
            "dco" in s.get("context", "").lower() and s["state"] == "success"
            for s in statuses
        )

        return {
            "has_tests": any(
                any(p.search(f["filename"]) for p in test_re) for f in files
            ),
            "has_linked_issue": bool(
                re.search(r"(?:closes|fixes|resolves)\s+#\d+", body, re.I)
            ),
            "has_description": len(body.strip()) >= 50,
            "dco_signed": dco_ok,
            "review_count": len([r for r in reviews if r.get("state") == "APPROVED"]),
            "small_diff": (pr.get("additions", 0) + pr.get("deletions", 0)) < 400,
        }

    @staticmethod
    def _compute_score(signals: dict, weights: dict) -> float:
        score = 0.0
        for key, weight in weights.items():
            value = signals.get(key, False)
            if key == "review_count":
                score += weight * min(value / 2.0, 1.0) * 100
            elif isinstance(value, bool):
                score += weight * (100 if value else 0)
        return min(max(score, 0), 100)

    @staticmethod
    def _build_health_comment(score: float, signals: dict, healthy_threshold: int) -> str:
        emoji = "🔴" if score < 50 else "🟡"
        rows = []
        labels = {
            "has_tests": "Test coverage",
            "has_linked_issue": "Linked issue",
            "has_description": "PR description (≥50 chars)",
            "dco_signed": "DCO sign-off",
            "review_count": "Approvals",
            "small_diff": "Diff size < 400 lines",
        }
        for key, label in labels.items():
            val = signals.get(key, False)
            if key == "review_count":
                status_str = f"{'✅' if val >= 1 else '⬜'} {val} approval(s)"
            else:
                status_str = "✅" if val else "❌"
            rows.append(f"| {status_str} | {label} |")

        table = "\n".join(rows)
        return f"""## {emoji} PR Health Score: {score:.0f}/100

This PR's health score is below the **{healthy_threshold}/100** threshold for a healthy label.

| Status | Signal |
|--------|--------|
{table}

Improving these signals will help reviewers engage faster and raise your score. 💪"""

    # ── Dashboard query ────────────────────────────────────────

    @staticmethod
    async def get_recent_scores(db: AsyncSession, owner: str, repo: str, limit: int = 20):
        result = await db.execute(
            select(PRHealthScore)
            .where(PRHealthScore.owner == owner, PRHealthScore.repo == repo)
            .order_by(PRHealthScore.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    @staticmethod
    async def get_average_score(db: AsyncSession, owner: str, repo: str) -> float:
        from sqlalchemy import func
        result = await db.execute(
            select(func.avg(PRHealthScore.score))
            .where(PRHealthScore.owner == owner, PRHealthScore.repo == repo)
        )
        avg = result.scalar()
        return round(float(avg), 1) if avg else 0.0
