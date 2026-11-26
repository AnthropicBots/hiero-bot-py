"""Seed a repository's counters one committed page at a time."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, AccountRepo
from app.github.client import GitHubClient
from app.utils.logger import get_logger
from app.workflows.counters import record_merge, record_review

log = get_logger("contributions.backfill")
PULLS_PER_PAGE = 25
REVIEWS_PER_PAGE = 100


async def backfill_contribution_counts(
    gh: GitHubClient, sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        targets = (await db.execute(select(
            AccountRepo.id, Account.org_login, AccountRepo.repo_name,
            Account.github_installation_id, AccountRepo.contribution_backfill_page,
        ).join(Account).where(
            Account.suspended_at.is_(None), AccountRepo.contribution_backfill_page > 0,
        ).order_by(AccountRepo.id))).all()

    for repo_id, owner, repo, installation_id, page in targets:
        try:
            await _backfill_page(gh, sessions, repo_id, owner, repo, installation_id, page)
        except Exception:
            log.exception("Could not backfill contributions for %s/%s page %d", owner, repo, page)


async def _backfill_page(
    gh: GitHubClient,
    sessions: async_sessionmaker[AsyncSession],
    repo_id: int,
    owner: str,
    repo: str,
    installation_id: int,
    page: int,
) -> None:
    pulls_path = f"/repos/{owner}/{repo}/pulls"
    pulls = await gh.get(pulls_path, installation_id, params={
        "state": "all", "sort": "created", "direction": "asc",
        "per_page": PULLS_PER_PAGE, "page": page,
    })

    contributions: list[tuple[int, str | None, set[str]]] = []
    for pr in pulls:
        number = pr["number"]
        author = (pr.get("user") or {}).get("login")
        merged = False
        if author and pr.get("state") == "closed":
            if "merged_at" in pr:
                merged = pr["merged_at"] is not None
            else:
                detail = await gh.get(f"{pulls_path}/{number}", installation_id)
                merged = detail.get("merged") is True

        reviewers: set[str] = set()
        review_page = 1
        while True:
            reviews = await gh.get(f"{pulls_path}/{number}/reviews", installation_id, params={
                "per_page": REVIEWS_PER_PAGE, "page": review_page,
            })
            for review in reviews:
                reviewer = (review.get("user") or {}).get("login")
                if reviewer and review.get("submitted_at"):
                    reviewers.add(reviewer.lower())
            if len(reviews) < REVIEWS_PER_PAGE:
                break
            review_page += 1
        contributions.append((number, author if merged else None, reviewers))

    next_page = page + 1 if len(pulls) == PULLS_PER_PAGE else 0
    async with sessions.begin() as db:
        advanced = await db.execute(update(AccountRepo).where(
            AccountRepo.id == repo_id, AccountRepo.contribution_backfill_page == page,
        ).values(contribution_backfill_page=next_page))
        if not advanced.rowcount:
            return

        for number, author, reviewers in contributions:
            if author:
                await record_merge(db, owner, repo, number, author)
            for reviewer in reviewers:
                await record_review(db, owner, repo, number, reviewer)
