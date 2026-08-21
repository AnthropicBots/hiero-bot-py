# app/auth/sync.py — Sync user authorized accounts with TTL caching

from __future__ import annotations

import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import decrypt_token
from app.db.models import Account, AccountRepo, AccountUser, User, UserOAuthToken
from app.utils.logger import get_logger

log = get_logger("auth.sync")

# In-memory cache for authorized account list per user: { user_id: (timestamp, list[dict]) }
_SYNC_CACHE: dict[int, tuple[float, list[dict]]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


def _prune_expired_cache(now: float) -> None:
    expired_keys = [uid for uid, (t, _) in _SYNC_CACHE.items() if now - t > CACHE_TTL_SECONDS * 2]
    for uid in expired_keys:
        _SYNC_CACHE.pop(uid, None)


def clear_sync_cache() -> None:
    _SYNC_CACHE.clear()


async def get_user_authorized_accounts(
    user: User,
    db: AsyncSession,
    force_sync: bool = False,
) -> list[dict]:
    now = time.time()
    _prune_expired_cache(now)
    if not force_sync and user.id in _SYNC_CACHE:
        cached_time, cached_accounts = _SYNC_CACHE[user.id]
        if now - cached_time < CACHE_TTL_SECONDS:
            return cached_accounts

    # Attempt to sync with GitHub API if user has an encrypted OAuth token
    token_stmt = select(UserOAuthToken).where(UserOAuthToken.user_id == user.id)
    token_res = await db.execute(token_stmt)
    user_token = token_res.scalar_one_or_none()

    if user_token and user_token.encrypted_access_token:
        try:
            token = decrypt_token(user_token.encrypted_access_token)
            if token:
                async with httpx.AsyncClient() as client:
                    res = await client.get(
                        "https://api.github.com/user/installations",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/vnd.github.v3+json",
                            "User-Agent": "Hiero-Bot-Py",
                        },
                    )
                    if res.status_code == 200:
                        installations = res.json().get("installations", [])
                        inst_ids = [inst["id"] for inst in installations if "id" in inst]

                        if inst_ids:
                            acc_stmt = select(Account).where(Account.github_installation_id.in_(inst_ids))
                            acc_res = await db.execute(acc_stmt)
                            accounts = acc_res.scalars().all()

                            for acc in accounts:
                                au_stmt = select(AccountUser).where(
                                    AccountUser.account_id == acc.id,
                                    AccountUser.user_id == user.id,
                                )
                                au_res = await db.execute(au_stmt)
                                au = au_res.scalar_one_or_none()
                                if not au:
                                    au = AccountUser(account_id=acc.id, user_id=user.id, authorized=True)
                                    db.add(au)
                                else:
                                    au.authorized = True
                            await db.commit()
        except Exception as e:
            log.warning("Error syncing user installations from GitHub API: %s", e)

    # Query DB for authorized accounts
    stmt = (
        select(Account)
        .join(AccountUser, Account.id == AccountUser.account_id)
        .where(AccountUser.user_id == user.id, AccountUser.authorized == True)
    )
    db_res = await db.execute(stmt)
    accounts = db_res.scalars().all()

    if not accounts:
        _SYNC_CACHE[user.id] = (now, [])
        return []

    account_ids = [acc.id for acc in accounts]
    repo_stmt = select(AccountRepo).where(AccountRepo.account_id.in_(account_ids))
    repo_res = await db.execute(repo_stmt)
    repo_rows = repo_res.scalars().all()

    repos_by_account: dict[int, list[str]] = {acc_id: [] for acc_id in account_ids}
    for r in repo_rows:
        repos_by_account[r.account_id].append(r.repo_name)

    formatted = [
        {
            "id": acc.id,
            "github_installation_id": acc.github_installation_id,
            "org_login": acc.org_login,
            "plan_tier": acc.plan_tier,
            "repos": repos_by_account.get(acc.id, []),
        }
        for acc in accounts
    ]

    _SYNC_CACHE[user.id] = (now, formatted)
    return formatted
