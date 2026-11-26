"""Persist distinct contributions inside the caller's transaction."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CountedMerge, CountedReview


async def _record(db: AsyncSession, model, **values: str | int) -> None:
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = postgresql_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise ValueError(f"Unsupported contribution-counter database: {dialect}")
    table = model.__table__
    await db.execute(
        insert(table).values(**values).on_conflict_do_nothing(
            index_elements=list(table.primary_key.columns),
        )
    )


async def record_merge(db: AsyncSession, owner: str, repo: str, number: int, author: str) -> None:
    await _record(
        db, CountedMerge, owner=owner.lower(), repo=repo.lower(),
        pr_number=number, author=author.lower(),
    )


async def record_review(db: AsyncSession, owner: str, repo: str, number: int, reviewer: str) -> None:
    await _record(
        db, CountedReview, owner=owner.lower(), repo=repo.lower(),
        pr_number=number, reviewer=reviewer.lower(),
    )


async def record_contribution_event(
    db: AsyncSession, owner: str, repo: str, event: str, payload: dict,
) -> bool:
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    if type(number) is not int or number < 1:
        return False

    action = payload.get("action")
    if (event, action) == ("pull_request", "closed") and pr.get("merged") is True:
        author = (pr.get("user") or {}).get("login")
        if author:
            await record_merge(db, owner, repo, number, author)
            return True
    elif (event, action) == ("pull_request_review", "submitted"):
        review = payload.get("review") or {}
        reviewer = (review.get("user") or {}).get("login")
        if reviewer:
            await record_review(db, owner, repo, number, reviewer)
            return True
    return False


async def contribution_counts(
    db: AsyncSession, owner: str, repo: str, login: str,
) -> tuple[int, int]:
    owner, repo, login = owner.lower(), repo.lower(), login.lower()
    merges = await db.scalar(select(func.count()).select_from(CountedMerge).where(
        CountedMerge.owner == owner, CountedMerge.repo == repo, CountedMerge.author == login,
    ))
    reviews = await db.scalar(select(func.count()).select_from(CountedReview).where(
        CountedReview.owner == owner, CountedReview.repo == repo, CountedReview.reviewer == login,
    ))
    return merges or 0, reviews or 0
