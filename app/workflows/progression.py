# app/workflows/progression.py

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ContributorSnapshot
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.progression")

DAYS_PER_MONTH = 30


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp, returning None on anything unusable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _months_since(start: datetime | None) -> int:
    if start is None:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - start).days
    return max(0, days // DAYS_PER_MONTH)


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
        """
        Gather a contributor's merged-PR count, reviews given, and tenure.

        Prefers the search API — one request answers a question that would
        otherwise need the repo's entire PR history — and falls back to
        paginated REST when search is unavailable or rate limited.
        """
        stats = await self._collect_stats_via_search(owner, repo, login, inst)
        if stats is None:
            stats = await self._collect_stats_via_rest(owner, repo, login, inst)
        return stats

    async def _collect_stats_via_search(
        self, owner: str, repo: str, login: str, inst: int
    ) -> dict | None:
        slug = f"{owner}/{repo}"
        try:
            merged = await self._gh.search_issues(
                f"repo:{slug} type:pr author:{login} is:merged",
                inst,
                per_page=1,
                sort="created",
                order="asc",
            )
            reviewed = await self._gh.search_issues(
                f"repo:{slug} type:pr reviewed-by:{login}",
                inst,
                per_page=1,
            )
        except Exception as exc:
            log.warning("Search-based stats unavailable for @%s: %s", login, exc)
            return None

        merged_prs = int(merged.get("total_count") or 0)

        # `sort=created&order=asc&per_page=1` puts the contributor's earliest
        # merged PR first, which dates the start of their involvement.
        first_contribution = None
        items = merged.get("items") or []
        if items:
            first_contribution = _parse_ts(
                items[0].get("closed_at") or items[0].get("created_at")
            )

        return {
            "merged_prs": merged_prs,
            "reviews_given": int(reviewed.get("total_count") or 0),
            "months_active": _months_since(first_contribution),
            "login": login,
            "source": "search",
        }

    async def _collect_stats_via_rest(
        self, owner: str, repo: str, login: str, inst: int
    ) -> dict:
        merged_prs = 0
        first_contribution: datetime | None = None

        try:
            # #41: this listing used to stop at the first 100 closed PRs, so on
            # any busy repo a long-standing contributor's merged count silently
            # capped out (usually at 0, since page one is the most recent PRs).
            prs = await self._gh.paginate(
                f"/repos/{owner}/{repo}/pulls",
                inst,
                params={"state": "closed"},
            )
            merged = [
                p
                for p in prs
                if (p.get("user") or {}).get("login") == login and p.get("merged_at")
            ]
            merged_prs = len(merged)

            dates = [_parse_ts(p["merged_at"]) for p in merged]
            dates = [d for d in dates if d]
            if dates:
                first_contribution = min(dates)
        except Exception as exc:
            log.warning("Could not read PR history for @%s: %s", login, exc)

        return {
            "merged_prs": merged_prs,
            "reviews_given": await self._count_reviews_via_rest(
                owner, repo, login, inst
            ),
            "months_active": _months_since(first_contribution),
            "login": login,
            "source": "rest",
        }

    async def _count_reviews_via_rest(
        self, owner: str, repo: str, login: str, inst: int
    ) -> int:
        """
        Count distinct pull requests this contributor reviewed.

        #41: the previous implementation counted rows from
        `/pulls/comments`, so a single thorough review that left eight inline
        comments scored as eight reviews, and a plain approve-with-no-comments
        scored as zero. Collapsing to distinct pull requests is a far closer
        answer to "how many reviews has this person given".
        """
        try:
            comments = await self._gh.paginate(
                f"/repos/{owner}/{repo}/pulls/comments", inst
            )
        except Exception as exc:
            log.warning("Could not read review comments for @%s: %s", login, exc)
            return 0

        reviewed_prs = {
            comment.get("pull_request_url")
            for comment in comments
            if (comment.get("user") or {}).get("login") == login
            and comment.get("pull_request_url")
        }
        return len(reviewed_prs)

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
