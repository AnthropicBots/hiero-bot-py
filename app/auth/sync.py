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


async def _fetch_installations(
    client: httpx.AsyncClient, token: str
) -> tuple[dict[int, dict], bool]:
    """
    Return the installations the user can access as a dict of {inst_id: inst_data}
    and whether that list is complete. It is incomplete when a page fails or the page cap
    is reached, and callers must not revoke access based on an incomplete list.
    """
    installations: dict[int, dict] = {}
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
            return installations, False

        batch = res.json().get("installations", [])
        for inst in batch:
            if isinstance(inst, dict) and "id" in inst:
                installations[inst["id"]] = inst
        if len(batch) < INSTALLATIONS_PER_PAGE:
            return installations, True

    log.warning(
        "User installation list exceeded %d pages; skipping revocation",
        MAX_INSTALLATION_PAGES,
    )
    return installations, False


async def _fetch_installation_ids(
    client: httpx.AsyncClient, token: str
) -> tuple[set[int], bool]:
    installations, complete = await _fetch_installations(client, token)
    return set(installations.keys()), complete


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
                    inst_map, complete = await _fetch_installations(client, token)
                    inst_ids = set(inst_map.keys())

                # Every account link this user has, loaded once and used both
                # to grant and to revoke, rather than one query per account.
                links_stmt = (
                    select(AccountUser, Account.github_installation_id)
                    .join(Account, Account.id == AccountUser.account_id)
                    .where(AccountUser.user_id == user.id)
                )
                links = {
                    au.account_id: (au, installation_id)
                    for au, installation_id in (await db.execute(links_stmt)).all()
                }

                if inst_ids:
                    acc_stmt = select(Account).where(Account.github_installation_id.in_(inst_ids))
                    acc_res = await db.execute(acc_stmt)
                    accounts = list(acc_res.scalars().all())
                    existing_inst_ids = {acc.github_installation_id for acc in accounts}

                    # Issue #128: If an installation in GitHub is missing from DB, create it
                    missing_inst_ids = inst_ids - existing_inst_ids
                    for missing_id in sorted(missing_inst_ids):
                        inst_payload = inst_map.get(missing_id, {})
                        account_data = inst_payload.get("account")
                        if not isinstance(account_data, dict) or not account_data.get("login"):
                            continue

                        org_login = account_data["login"]
                        github_account_id = (
                            account_data.get("id")
                            if isinstance(account_data.get("id"), int)
                            else None
                        )
                        account_type = account_data.get("type") or "Organization"

                        new_acc = Account(
                            github_installation_id=missing_id,
                            github_account_id=github_account_id,
                            org_login=org_login,
                            account_type=account_type,
                            plan_tier="free",
                            suspended_at=None,
                        )
                        db.add(new_acc)
                        await db.flush()
                        accounts.append(new_acc)

                        # Fetch repos for the newly created installation
                        try:
                            repos_url = f"https://api.github.com/user/installations/{missing_id}/repositories"
                            repos_res = await client.get(
                                repos_url,
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github.v3+json",
                                    "User-Agent": "Hiero-Bot-Py",
                                },
                            )
                            if repos_res.status_code == 200:
                                repos_data = repos_res.json().get("repositories", [])
                                for repo_obj in repos_data:
                                    if isinstance(repo_obj, dict):
                                        rname = repo_obj.get("name")
                                        if rname and isinstance(rname, str):
                                            db.add(
                                                AccountRepo(
                                                    account_id=new_acc.id,
                                                    repo_name=rname.strip(),
                                                )
                                            )
                        except Exception as err:
                            log.warning(
                                "Failed fetching repositories for installation %s: %s",
                                missing_id,
                                err,
                            )

                    for acc in accounts:
                        link = links.get(acc.id)
                        if link is None:
                            db.add(AccountUser(account_id=acc.id, user_id=user.id, authorized=True))
                        else:
                            link[0].authorized = True

                # #147: GitHub no longer lists installations the user was
                # removed from, so revoke those. Only a complete list is
                # trusted; a failed or truncated read leaves access as is.
                if complete:
                    for au, installation_id in links.values():
                        if au.authorized and installation_id not in inst_ids:
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
    accounts = list(db_res.scalars().all())

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
