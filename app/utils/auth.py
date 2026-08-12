from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("utils.auth")
_security = HTTPBasic(auto_error=False)


def _auth_configured() -> bool:
    return bool(settings.dashboard_username and settings.dashboard_password)


async def require_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> None:
    if not settings.legacy_basic_auth_enabled or not _auth_configured():
        return

    unauthorized = HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )
    if credentials is None:
        raise unauthorized

    user_ok = hmac.compare_digest(credentials.username, settings.dashboard_username)
    pass_ok = hmac.compare_digest(credentials.password, settings.dashboard_password)
    if not (user_ok and pass_ok):
        raise unauthorized
