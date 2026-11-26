import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Account, AccountRepo
from app.workflows import contribution_backfill
from app.workflows.counters import (
    contribution_counts,
    record_contribution_event,
    record_merge,
    record_review,
)


@pytest.mark.asyncio
async def test_contribution_counters_deduplicate_retries_and_reviewers(db):
    await record_merge(db, "Org", "Repo", 7, "Alice")
    await record_merge(db, "org", "repo", 7, "alice")
    await record_merge(db, "org", "another-repo", 7, "alice")
    await record_review(db, "org", "repo", 8, "ALICE")
    await record_review(db, "org", "repo", 8, "alice")
    await record_review(db, "org", "repo", 8, "bob")
    await record_review(db, "org", "repo", 9, "alice")
    await db.commit()

    assert await contribution_counts(db, "Org", "repo", "Alice") == (1, 2)
    assert await contribution_counts(db, "org", "repo", "bob") == (0, 1)
    assert await contribution_counts(db, "org", "another-repo", "alice") == (1, 0)


@pytest.mark.asyncio
async def test_contribution_event_counts_only_merged_and_submitted(db):
    pull = {"number": 42, "user": {"login": "Alice"}, "merged": True}
    review = {"user": {"login": "Bob"}}
    for _ in range(2):
        assert await record_contribution_event(
            db, "org", "repo", "pull_request", {"action": "closed", "pull_request": pull},
        )
        assert await record_contribution_event(
            db, "org", "repo", "pull_request_review",
            {"action": "submitted", "pull_request": pull, "review": review},
        )
    assert not await record_contribution_event(
        db, "org", "repo", "pull_request", {"action": "opened", "pull_request": pull},
    )
    assert not await record_contribution_event(
        db, "org", "repo", "pull_request_review",
        {"action": "dismissed", "pull_request": pull, "review": review},
    )
    assert await contribution_counts(db, "org", "repo", "alice") == (1, 0)
    assert await contribution_counts(db, "org", "repo", "bob") == (0, 1)


@pytest.mark.asyncio
async def test_backfill_retries_failed_page_and_resumes_without_duplicates(db, monkeypatch):
    monkeypatch.setattr(contribution_backfill, "PULLS_PER_PAGE", 1)
    monkeypatch.setattr(contribution_backfill, "REVIEWS_PER_PAGE", 2)
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    account = Account(
        github_installation_id=17, github_account_id=23, org_login="Org", account_type="Organization",
    )
    db.add(account)
    await db.flush()
    db.add(AccountRepo(account_id=account.id, repo_name="Repo"))
    await db.commit()

    class GitHubHistory:
        def __init__(self):
            self.fail_second_review_page = True
            self.pulled_pages = []

        async def get(self, path, installation_id, *, params=None):
            assert installation_id == 17
            if path == "/repos/Org/Repo/pulls":
                page = params["page"]
                self.pulled_pages.append(page)
                if page < 3:
                    return [{"number": page, "state": "closed", "user": {"login": "Alice"}}]
                return []
            if path == "/repos/Org/Repo/pulls/1":
                return {"merged": True}
            if path == "/repos/Org/Repo/pulls/2":
                return {"merged": False}
            if path == "/repos/Org/Repo/pulls/2/reviews":
                return []
            assert path == "/repos/Org/Repo/pulls/1/reviews"
            if params["page"] == 1:
                return [
                    {"user": {"login": "Alice"}, "submitted_at": "2026-01-01"},
                    {"user": {"login": "Pending draft"}},
                ]
            if self.fail_second_review_page:
                raise RuntimeError("temporary review history error")
            return [{"user": {"login": "ALICE"}, "submitted_at": "2026-01-02"}]

    gh = GitHubHistory()
    await contribution_backfill.backfill_contribution_counts(gh, sessions)
    async with sessions() as check:
        assert await check.scalar(select(AccountRepo.contribution_backfill_page)) == 1
        assert await contribution_counts(check, "org", "repo", "alice") == (0, 0)

    # A live event can race with the historical read.
    await record_merge(db, "org", "repo", 1, "alice")
    await db.commit()
    gh.fail_second_review_page = False
    for _ in range(4):
        await contribution_backfill.backfill_contribution_counts(gh, sessions)

    assert gh.pulled_pages == [1, 1, 2, 3]
    async with sessions() as check:
        assert await check.scalar(select(AccountRepo.contribution_backfill_page)) == 0
        assert await contribution_counts(check, "org", "repo", "alice") == (1, 1)
        assert await contribution_counts(check, "org", "repo", "pending draft") == (0, 0)
