# app/workflows/progression.py

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from app.github.client import GitHubClient
from app.db.models import ContributorSnapshot
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.progression")

MILESTONES = {
    1: "🎊 **First merged PR** in this repo — welcome to the Hiero contributor community!",
    5: "🌟 **5 merged PRs** — you're building real momentum!",
    10: "🚀 **10 merged PRs** — you're officially a regular contributor!",
    25: "💎 **25 merged PRs** — incredible dedication to the Hiero ecosystem!",
    50: "🏆 **50 merged PRs** — one of our most committed contributors ever!",
}


class ProgressionWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    async def handle_merged_pr(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.progression
        if not cfg.enabled:
            return

        pr = payload.get("pull_request", {})
        if not pr.get("merged_at"):
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db: AsyncSession = ctx["db"]
        pr_number = pr["number"]
        login = pr["user"]["login"]

        stats = await self._collect_stats(owner, repo, login, inst)

        # Milestone celebration
        if cfg.celebrate_milestones and stats["merged_prs"] in MILESTONES:
            msg = MILESTONES[stats["merged_prs"]]
            await self._gh.post_comment(owner, repo, pr_number,
                                        f"@{login} {msg}", inst)

        # Recommend next issues
        if cfg.recommend_issues_after_merge:
            await self._recommend_issues(ctx, pr_number, login, cfg.recommendation_count)

        # Check & announce role eligibility
        eligible_for = self._check_eligibility(stats, cfg)
        if eligible_for:
            await self._gh.post_comment(
                owner, repo, pr_number,
                self._build_eligibility_notice(login, eligible_for), inst,
            )

        # Persist contributor snapshot
        snapshot = ContributorSnapshot(
            owner=owner,
            repo=repo,
            login=login,
            merged_prs=stats["merged_prs"],
            reviews_given=stats["reviews_given"],
            months_active=stats["months_active"],
            current_role="contributor",
            eligible_for=eligible_for,
        )
        db.add(snapshot)

        await audit.record(
            db, action="contributor.role_suggested" if eligible_for else "workflow.skipped",
            owner=owner, repo=repo, target_login=login, target_number=pr_number,
            reason=f"Post-merge check: eligible_for={eligible_for}",
            metadata=stats,
        )
        await db.commit()

    async def check_and_report(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.progression
        if not cfg.enabled:
            return

        login = (payload.get("comment") or {}).get("user", {}).get("login", "")
        issue_number = (payload.get("issue") or {}).get("number")
        if not login or not issue_number:
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        stats = await self._collect_stats(owner, repo, login, inst)

        report = self._build_full_report(login, stats, cfg)
        await self._gh.post_comment(owner, repo, issue_number, report, inst)

        await audit.record(
            ctx["db"], action="contributor.role_suggested",
            owner=owner, repo=repo, target_login=login, target_number=issue_number,
            reason="User invoked /check-eligibility",
            metadata=stats,
        )
        await ctx["db"].commit()

    # ── Helpers ───────────────────────────────────────────────

    async def _collect_stats(
        self, owner: str, repo: str, login: str, inst: int
    ) -> dict:
        merged_prs = 0
        reviews_given = 0
        months_active = 0
        first_contribution: datetime | None = None

        try:
            prs = await self._gh.get(
                f"/repos/{owner}/{repo}/pulls", inst,
                params={"state": "closed", "per_page": 100}
            )
            merged = [p for p in (prs or [])
                      if p.get("user", {}).get("login") == login and p.get("merged_at")]
            merged_prs = len(merged)
            if merged:
                dates = [
                    datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
                    for p in merged
                ]
                first_contribution = min(dates)
        except Exception:
            pass

        try:
            review_comments = await self._gh.get(
                f"/repos/{owner}/{repo}/pulls/comments", inst,
                params={"per_page": 100}
            )
            reviews_given = sum(
                1 for c in (review_comments or [])
                if (c.get("user") or {}).get("login") == login
            )
        except Exception:
            pass

        if first_contribution:
            months_active = max(0, int(
                (datetime.now(timezone.utc) - first_contribution).days / 30
            ))

        return {
            "merged_prs": merged_prs,
            "reviews_given": reviews_given,
            "months_active": months_active,
            "login": login,
        }

    @staticmethod
    def _check_eligibility(stats: dict, cfg) -> str | None:
        """Return the highest role the contributor is eligible for, or None."""
        for role, reqs in [
            ("maintainer", cfg.requirements_for_maintainer),
            ("committer", cfg.requirements_for_committer),
            ("junior-committer", cfg.requirements_for_junior_committer),
        ]:
            if (stats["merged_prs"] >= reqs.min_merged_prs
                    and stats["reviews_given"] >= reqs.min_reviews_given
                    and stats["months_active"] >= reqs.min_months_active):
                return role
        return None

    @staticmethod
    def _build_eligibility_notice(login: str, role: str) -> str:
        return (
            f"🎉 @{login} — based on your contributions you may now be eligible for the "
            f"**{role}** role!\n\n"
            f"Ask a maintainer to review your nomination. "
            f"Use `/check-eligibility` to see the full breakdown."
        )

    @staticmethod
    def _build_full_report(login: str, stats: dict, cfg) -> str:
        def row(role: str, reqs) -> str:
            missing = []
            if stats["merged_prs"] < reqs.min_merged_prs:
                missing.append(f"{reqs.min_merged_prs - stats['merged_prs']} more PRs")
            if stats["reviews_given"] < reqs.min_reviews_given:
                missing.append(f"{reqs.min_reviews_given - stats['reviews_given']} more reviews")
            if stats["months_active"] < reqs.min_months_active:
                missing.append(f"{reqs.min_months_active - stats['months_active']} more months")
            eligible = len(missing) == 0
            detail = "Meets all requirements!" if eligible else "; ".join(missing)
            return f"| **{role}** | {'✅ Eligible' if eligible else '⏳ Not yet'} | {detail} |"

        rows = "\n".join([
            row("junior-committer", cfg.requirements_for_junior_committer),
            row("committer", cfg.requirements_for_committer),
            row("maintainer", cfg.requirements_for_maintainer),
        ])

        return f"""## 📊 Progression Report for @{login}

**Your stats in this repo:**
- 📦 Merged PRs: **{stats['merged_prs']}**
- 👀 Reviews given: **{stats['reviews_given']}**
- 📅 Months active: **{stats['months_active']}**

**Role eligibility:**

| Role | Status | Details |
|------|--------|---------|
{rows}

> 💡 Once you meet the requirements, ask a maintainer to nominate you for the next role!"""

    async def _recommend_issues(
        self, ctx: dict, pr_number: int, login: str, count: int
    ) -> None:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        label = ctx["config"].difficulty_labels.intermediate
        try:
            issues = await self._gh.list_issues(
                owner, repo, inst,
                state="open", labels=label, assignee="none"
            )
            issues = [i for i in issues if not i.get("pull_request")][:count]
            if not issues:
                return
            issue_list = "\n".join(
                f"- [#{i['number']} — {i['title']}]({i['html_url']})" for i in issues
            )
            await self._gh.post_comment(
                owner, repo, pr_number,
                f"🎉 Great work @{login}! Here are some suggested next issues:\n\n"
                f"{issue_list}\n\nUse `/assign` to pick one up!",
                inst,
            )
        except Exception as exc:
            log.warning("Issue recommendation failed: %s", exc)
