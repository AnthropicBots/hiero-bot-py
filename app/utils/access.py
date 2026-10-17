# app/utils/access.py — Tenant isolation & access control logic

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.sync import get_user_authorized_accounts
from app.db.database import get_db
from app.db.models import User
from app.utils.logger import get_logger

log = get_logger("utils.access")


class AuthorizedAccess:
    def __init__(self, owners: set[str], repos: dict[str, set[str]]) -> None:
        self.owners = owners
        self.repos = repos


async def get_authorized_access(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuthorizedAccess:
    accounts = await get_user_authorized_accounts(user, db)
    owners = {acc["org_login"] for acc in accounts if acc.get("org_login")}
    repos: dict[str, set[str]] = {}
    for acc in accounts:
        org = acc.get("org_login")
        if not org:
            continue
        names = acc.get("repos") or []
        if org not in repos:
            repos[org] = set()
        repos[org].update(names)
    return AuthorizedAccess(owners, repos)


async def get_authorized_owners(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> set[str]:
    access = await get_authorized_access(user, db)
    return access.owners


async def get_authorized_repos(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, set[str]]:
    access = await get_authorized_access(user, db)
    return access.repos


def verify_repo_access(
    owner: str,
    repo: str | None,
    allowed_owners: set[str],
    allowed_repos: dict[str, set[str]],
) -> None:
    if not repo:
        return
    if owner not in allowed_owners:
        return
    known = allowed_repos.get(owner)
    if not known:
        # Installations created before AccountRepo tracking have no repo rows.
        # Allow org-level access in that legacy case so existing users are not
        # locked out; once sync populates repos this becomes enforcing.
        log.debug("No recorded repos for org '%s', allowing repo '%s' (legacy)", owner, repo)
        return
    if repo not in known:
        log.warning("User attempted unauthorized access to repo '%s/%s'", owner, repo)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to repository '{owner}/{repo}'",
        )


def verify_owner_access(
    owner: str,
    user: User,
    allowed_owners: set[str],
) -> None:
    if owner not in allowed_owners:
        log.warning("User @%s attempted unauthorized access to org '%s'", user.github_login, owner)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to organization '{owner}'",
        )
