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


def _matches(supplied: str, expected: str) -> bool:
    """
    Constant-time comparison of two credentials.

    Compared as UTF-8 bytes rather than as str, because `hmac.compare_digest`
    raises TypeError on non-ASCII str input. FastAPI's `HTTPBasic` currently
    decodes the header as ASCII and rejects anything else before it reaches
    here, so this is defence in depth, not a live bug fix — but the inputs are
    client-controlled and this function should not depend on an upstream
    decoding detail to avoid raising.
    """
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


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

    # Both comparisons always run — short-circuiting on the username would
    # leak, by response time, whether the username alone was correct.
    user_ok = _matches(credentials.username, settings.dashboard_username)
    pass_ok = _matches(credentials.password, settings.dashboard_password)
    if not (user_ok and pass_ok):
        raise unauthorized
