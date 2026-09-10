# scripts/backfill_installations.py — One-off script to backfill Account & AccountRepo from GitHub App API

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from sqlalchemy import select

from app.db.database import AsyncSessionLocal, init_db
from app.db.models import Account, AccountRepo
from app.github.client import GitHubClient
from app.utils.logger import get_logger

log = get_logger("scripts.backfill")


async def backfill():
    log.info("Starting installation backfill script...")
    await init_db()

    gh = GitHubClient()
    try:
        installations = await gh.list_installations()
        log.info("Found %d installations on GitHub App", len(installations))

        async with AsyncSessionLocal() as db:
            for inst in installations:
                inst_id = inst["id"]
                account_info = inst.get("account", {})
                org_login = account_info.get("login", "")
                github_account_id = account_info.get("id")
                account_type = account_info.get("type", "Organization")

                stmt = select(Account).where(Account.github_installation_id == inst_id)
                res = await db.execute(stmt)
                acc = res.scalar_one_or_none()

                if acc:
                    acc.org_login = org_login
                    acc.github_account_id = github_account_id
                    acc.account_type = account_type
                    acc.suspended_at = None
                else:
                    acc = Account(
                        github_installation_id=inst_id,
                        github_account_id=github_account_id,
                        org_login=org_login,
                        account_type=account_type,
                        plan_tier="free",
                    )
                    db.add(acc)

                await db.commit()
                await db.refresh(acc)

                # Fetch repos for this installation
                try:
                    repos = await gh.list_installation_repos(inst_id)
                    for repo_name in repos:
                        repo_stmt = select(AccountRepo).where(
                            AccountRepo.account_id == acc.id,
                            AccountRepo.repo_name == repo_name,
                        )
                        repo_res = await db.execute(repo_stmt)
                        if not repo_res.scalar_one_or_none():
                            db.add(AccountRepo(account_id=acc.id, repo_name=repo_name))
                    await db.commit()
                    log.info("Backfilled installation %d (%s) with %d repos", inst_id, org_login, len(repos))
                except Exception as e:
                    log.warning("Could not fetch repos for installation %d: %s", inst_id, e)

        log.info("Backfill complete!")
    finally:
        await gh.close()


if __name__ == "__main__":
    asyncio.run(backfill())
