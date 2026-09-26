# app/workflows/progression.py

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, ContributorSnapshot
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger
from app.workflows.onboarding import looks_like_bot

log = get_logger("workflow.progression")

DAYS_PER_MONTH = 30

# #122: a progression check runs inside the webhook request and any commenter
# can trigger one, so the GitHub calls it makes must be bounded. Past these
# caps the stats are reported as a lower bound (`partial`) instead of crawling.
MAX_REVIEWED_PRS_INSPECTED = 50
MAX_REST_PR_PAGES = 10
REVIEW_FETCH_CONCURRENCY = 5

# Repeated /check-eligibility calls reuse recent stats instead of recomputing.
_STATS_CACHE_TTL = 300  # 5 minutes
_MAX_STATS_CACHE_ENTRIES = 512
_stats_cache: OrderedDict[tuple[str, str, str], tuple[float, dict]] = OrderedDict()

ROLE_ORDER = ("contributor", "junior-committer", "committer", "maintainer")

# Maps GitHub's repository permission to the role it implies. /label uses the
# same split: write and above are committers or maintainers.
PERMISSION_ROLES = {
    "admin": "maintainer",
    "maintain": "maintainer",
    "write": "committer",
}


def clear_stats_cache() -> None:
    _stats_cache.clear()


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
        author = pr.get("user") or {}
        login = author.get("login") or ""

        if not login or looks_like_bot(login, author.get("type") or ""):
            log.info("Skipping progression for bot-authored PR #%s (@%s)", pr_number, login)
            return

        # A merge must see fresh counts: cached stats would repeat or miss
        # milestone celebrations when merges land close together.
        stats = await self._collect_stats(owner, repo, login, inst, use_cache=False)

        # Milestone celebration
        if cfg.celebrate_milestones and stats["merged_prs"] in MILESTONES:
            msg = MILESTONES[stats["merged_prs"]]
            await self._gh.post_comment(owner, repo, pr_number,
                                        f"@{login} {msg}", inst)

        # Recommend next issues
        if cfg.recommend_issues_after_merge:
            await self._recommend_issues(ctx, pr_number, login, cfg.recommendation_count)

        # Check & announce role eligibility — only for a role above the one the
        # contributor already holds, and only once per login and role.
        current_role = await self._current_role(owner, repo, login, inst)
        eligible_for = self._check_eligibility(stats, cfg)
        if eligible_for and ROLE_ORDER.index(eligible_for) <= ROLE_ORDER.index(current_role):
            eligible_for = None

        announced = False
        if eligible_for and not await self._already_suggested(
            db, owner, repo, login, eligible_for
        ):
            await self._gh.post_comment(
                owner, repo, pr_number,
                self._build_eligibility_notice(login, eligible_for), inst,
            )
            announced = True

        # Persist contributor snapshot
        snapshot = ContributorSnapshot(
            owner=owner,
            repo=repo,
            login=login,
            merged_prs=stats["merged_prs"],
            reviews_given=stats["reviews_given"],
            months_active=stats["months_active"],
            current_role=current_role,
            eligible_for=eligible_for,
        )
        db.add(snapshot)

        reason = f"Post-merge check: eligible_for={eligible_for}"
        if eligible_for and not announced:
            reason += " (already suggested)"
        await audit.record(
            db, action="contributor.role_suggested" if announced else "workflow.skipped",
            owner=owner, repo=repo, target_login=login, target_number=pr_number,
            reason=reason,
            metadata={**stats, "current_role": current_role, "eligible_for": eligible_for},
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

        eligible_for = self._check_eligibility(stats, cfg)

        await audit.record(
            ctx["db"],
            action="contributor.role_suggested" if eligible_for else "workflow.skipped",
            owner=owner, repo=repo, target_login=login, target_number=issue_number,
            reason="User invoked /check-eligibility",
            metadata={**stats, "eligible_for": eligible_for},
        )
        await ctx["db"].commit()

    # ── Helpers ───────────────────────────────────────────────

    async def _collect_stats(
        self, owner: str, repo: str, login: str, inst: int, *, use_cache: bool = True
    ) -> dict:
        """
        Gather a contributor's merged-PR count, reviews given, and tenure.

        Prefers the search API — one request answers a question that would
        otherwise need the repo's entire PR history — and falls back to
        paginated REST when search is unavailable or rate limited. Both paths
        are bounded; `partial` is set when a cap cut the count short.
        """
        key = (owner.lower(), repo.lower(), login.lower())

        if use_cache:
            cached = _stats_cache.get(key)
            if cached is not None and time.monotonic() < cached[0]:
                _stats_cache.move_to_end(key)
                return dict(cached[1])

        stats = await self._collect_stats_via_search(owner, repo, login, inst)
        if stats is None:
            stats = await self._collect_stats_via_rest(owner, repo, login, inst)

        _stats_cache[key] = (time.monotonic() + _STATS_CACHE_TTL, dict(stats))
        _stats_cache.move_to_end(key)
        while len(_stats_cache) > _MAX_STATS_CACHE_ENTRIES:
            _stats_cache.popitem(last=False)

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
        except Exception as exc:
            log.warning("Search-based merged-PR stats unavailable for @%s: %s", login, exc)
            return None

        merged_prs = int(merged.get("total_count") or 0)

        first_contribution = None
        items = merged.get("items") or []
        if items:
            first_contribution = _parse_ts(
                items[0].get("closed_at") or items[0].get("created_at")
            )

        try:
            reviews_given, partial = await self._count_reviews_via_search(
                owner, repo, login, inst
            )
        except Exception as exc:
            log.warning("Search-based review stats unavailable for @%s: %s", login, exc)
            return None

        return {
            "merged_prs": merged_prs,
            "reviews_given": reviews_given,
            "months_active": _months_since(first_contribution),
            "login": login,
            "source": "search",
            "partial": partial,
        }

    async def _count_reviews_via_search(
        self, owner: str, repo: str, login: str, inst: int
    ) -> tuple[int, bool]:
        """
        Count submitted review objects by using `reviewed-by:` only to find
        candidate pull requests, then inspecting the actual review objects.

        Only the most recently updated MAX_REVIEWED_PRS_INSPECTED candidates
        are inspected. Every candidate past the cap has at least one review by
        the contributor, so each counts as one and the total is a lower bound.
        """
        slug = f"{owner}/{repo}"

        result = await self._gh.search_issues(
            f"repo:{slug} type:pr reviewed-by:{login}",
            inst,
            per_page=MAX_REVIEWED_PRS_INSPECTED,
            sort="updated",
            order="desc",
        )
        items = (result.get("items") or [])[:MAX_REVIEWED_PRS_INSPECTED]
        total = max(int(result.get("total_count") or 0), len(items))

        pr_numbers = [item["number"] for item in items if item.get("number")]
        counts = await self._fetch_review_counts(owner, repo, pr_numbers, login, inst)

        # Search says the contributor reviewed each of these PRs, so a PR whose
        # reviews could not be read still counts once.
        reviews_given = sum(1 if count is None else count for count in counts)
        uninspected = total - len(pr_numbers)
        return reviews_given + uninspected, uninspected > 0

    async def _collect_stats_via_rest(
        self, owner: str, repo: str, login: str, inst: int
    ) -> dict:
        merged_prs = 0
        first_contribution: datetime | None = None
        partial = False

        try:
            # #41: this listing used to stop at the first 100 closed PRs, so on
            # any busy repo a long-standing contributor's merged count silently
            # capped out (usually at 0, since page one is the most recent PRs).
            # #122: it is still capped, but far higher, and flagged when hit.
            prs = await self._gh.paginate(
                f"/repos/{owner}/{repo}/pulls",
                inst,
                params={"state": "closed"},
                max_pages=MAX_REST_PR_PAGES,
            )
            partial = len(prs) >= MAX_REST_PR_PAGES * 100
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

        reviews_given, reviews_partial = await self._count_reviews_via_rest(
            owner, repo, login, inst
        )

        return {
            "merged_prs": merged_prs,
            "reviews_given": reviews_given,
            "months_active": _months_since(first_contribution),
            "login": login,
            "source": "rest",
            "partial": partial or reviews_partial,
        }

    async def _count_reviews_via_rest(
        self, owner: str, repo: str, login: str, inst: int
    ) -> tuple[int, bool]:
        """
        Count submitted reviews by the contributor.

        Without search there is no index of who reviewed what, so the fallback
        inspects the MAX_REVIEWED_PRS_INSPECTED most recently updated pull
        requests and counts every submitted review object authored by the
        contributor. Older history is not walked; the result is flagged as
        partial when more pull requests exist.
        """
        try:
            prs = await self._gh.paginate(
                f"/repos/{owner}/{repo}/pulls",
                inst,
                params={"state": "all", "sort": "updated", "direction": "desc"},
                per_page=MAX_REVIEWED_PRS_INSPECTED,
                max_pages=1,
            )
        except Exception as exc:
            log.warning("Could not read PR history for @%s: %s", login, exc)
            return 0, False

        pr_numbers = [
            pr["number"] for pr in prs[:MAX_REVIEWED_PRS_INSPECTED] if pr.get("number")
        ]
        counts = await self._fetch_review_counts(owner, repo, pr_numbers, login, inst)

        return (
            sum(count or 0 for count in counts),
            len(prs) >= MAX_REVIEWED_PRS_INSPECTED,
        )

    async def _fetch_review_counts(
        self, owner: str, repo: str, pr_numbers: list[int], login: str, inst: int
    ) -> list[int | None]:
        """
        Count the contributor's submitted reviews on each PR, a few at a time.

        A PR whose reviews could not be read yields None so the caller decides
        how to count it.
        """
        semaphore = asyncio.Semaphore(REVIEW_FETCH_CONCURRENCY)

        async def count(pr_number: int) -> int | None:
            async with semaphore:
                try:
                    reviews = await self._gh.list_pr_reviews(
                        owner, repo, pr_number, inst
                    )
                except Exception as exc:
                    log.warning(
                        "Could not read reviews for PR #%s (@%s): %s",
                        pr_number,
                        login,
                        exc,
                    )
                    return None

            return sum(
                1
                for review in reviews
                if (review.get("user") or {}).get("login") == login
            )

        return list(await asyncio.gather(*(count(n) for n in pr_numbers)))

    async def _current_role(
        self, owner: str, repo: str, login: str, inst: int
    ) -> str:
        """The role the contributor already holds, judged by repo permission."""
        try:
            permission = await self._gh.get_collaborator_permission(
                owner, repo, login, inst
            )
        except Exception as exc:
            log.warning("Could not read repo permission for @%s: %s", login, exc)
            return "contributor"
        return PERMISSION_ROLES.get(permission, "contributor")

    @staticmethod
    async def _already_suggested(
        db: AsyncSession, owner: str, repo: str, login: str, role: str
    ) -> bool:
        """True when the audit log shows this role was already suggested."""
        result = await db.execute(
            select(AuditLog).where(
                AuditLog.action == "contributor.role_suggested",
                AuditLog.owner == owner,
                AuditLog.repo == repo,
                AuditLog.target_login == login,
            )
        )
        for entry in result.scalars():
            # Entries written before #122 carry the role only in the reason.
            if (entry.metadata_json or {}).get("eligible_for") == role:
                return True
            if (entry.reason or "").endswith(f"eligible_for={role}"):
                return True
        return False

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

        partial_note = (
            "\n\n> ℹ️ Your history is large, so only part of it was inspected — "
            "these counts are a lower bound."
            if stats.get("partial") else ""
        )

        return f"""## 📊 Progression Report for @{login}

**Your stats in this repo:**
- 📦 Merged PRs: **{stats['merged_prs']}**
- 👀 Reviews given: **{stats['reviews_given']}**
- 📅 Months active: **{stats['months_active']}**

**Role eligibility:**

| Role | Status | Details |
|------|--------|---------|
{rows}

> 💡 Once you meet the requirements, ask a maintainer to nominate you for the next role!{partial_note}"""

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
