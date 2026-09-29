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

INSTALLATIONS_URL = "https://api.github.com/user/installations"
INSTALLATIONS_PER_PAGE = 100
# Revoking access needs the complete list. Past this many pages the list is
# treated as incomplete and nothing is revoked.
MAX_INSTALLATION_PAGES = 10


def _prune_expired_cache(now: float) -> None:
    expired_keys = [uid for uid, (t, _) in _SYNC_CACHE.items() if now - t > CACHE_TTL_SECONDS * 2]
    for uid in expired_keys:
        _SYNC_CACHE.pop(uid, None)


def clear_sync_cache() -> None:
    _SYNC_CACHE.clear()


async def _fetch_installation_ids(
    client: httpx.AsyncClient, token: str
) -> tuple[set[int], bool]:
    """
    Return the installation IDs the user can access and whether that list is
    complete. It is incomplete when a page fails or the page cap is reached,
    and callers must not revoke access based on an incomplete list.
    """
    ids: set[int] = set()
    for page in range(1, MAX_INSTALLATION_PAGES + 1):
        res = await client.get(
            INSTALLATIONS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Hiero-Bot-Py",
            },
            params={"per_page": INSTALLATIONS_PER_PAGE, "page": page},
        )
        if res.status_code != 200:
            return ids, False

        batch = res.json().get("installations", [])
        ids.update(inst["id"] for inst in batch if "id" in inst)
        if len(batch) < INSTALLATIONS_PER_PAGE:
            return ids, True

    log.warning(
        "User installation list exceeded %d pages; skipping revocation",
        MAX_INSTALLATION_PAGES,
    )
    return ids, False


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
                    inst_ids, complete = await _fetch_installation_ids(client, token)

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

                # #147: GitHub no longer lists installations the user was
                # removed from, so revoke those. Only a complete list is
                # trusted; a failed or truncated read leaves access as is.
                if complete:
                    held_stmt = (
                        select(AccountUser, Account.github_installation_id)
                        .join(Account, Account.id == AccountUser.account_id)
                        .where(
                            AccountUser.user_id == user.id,
                            AccountUser.authorized == True,
                        )
                    )
                    for au, installation_id in (await db.execute(held_stmt)).all():
                        if installation_id not in inst_ids:
                            au.authorized = False
                            log.info(
                                "Revoked user %s access to installation %s",
                                user.id,
                                installation_id,
                            )

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
