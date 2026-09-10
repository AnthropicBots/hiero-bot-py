from datetime import datetime, timezone

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import (
    SESSION_COOKIE_NAME,
    SESSION_EXPIRE_SECONDS,
    create_db_session,
    delete_db_session,
    encrypt_token,
    unsign_session_id,
)
from app.db.database import get_db
from app.db.models import User, UserOAuthToken
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("auth.oauth")
router = APIRouter(prefix="/auth", tags=["auth"])

STATE_COOKIE_NAME = "oauth_state"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"


from app.db.models import Account, AccountRepo, AccountUser


@router.get("/login")
async def github_oauth_login(request: Request, db: AsyncSession = Depends(get_db)):
    # Local dev fallback when client_id is not configured or set to mock in non-production
    if not settings.is_production and (not settings.github_oauth_client_id or "mock" in settings.github_oauth_client_id):
        log.info("Mock or unconfigured OAuth client ID detected — initiating local dev login...")
        
        # Upsert dev user
        stmt = select(User).where(User.github_login == "demo_maintainer")
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            user = User(
                github_user_id=99999,
                github_login="demo_maintainer",
                github_email="maintainer@hiero.local",
                avatar_url="https://avatars.githubusercontent.com/u/185269030?v=4",
                last_login_at=datetime.now(timezone.utc),
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        # Upsert dev account & repo
        acc_stmt = select(Account).where(Account.org_login == "AnthropicBots")
        acc_res = await db.execute(acc_stmt)
        acc = acc_res.scalar_one_or_none()
        if not acc:
            acc = Account(
                github_installation_id=12345,
                github_account_id=67890,
                org_login="AnthropicBots",
                account_type="Organization",
                plan_tier="free",
            )
            db.add(acc)
            await db.commit()
            await db.refresh(acc)

        au_stmt = select(AccountUser).where(AccountUser.account_id == acc.id, AccountUser.user_id == user.id)
        if not (await db.execute(au_stmt)).scalar_one_or_none():
            db.add(AccountUser(account_id=acc.id, user_id=user.id, authorized=True))

        ar_stmt = select(AccountRepo).where(AccountRepo.account_id == acc.id, AccountRepo.repo_name == "hiero-bot-py")
        if not (await db.execute(ar_stmt)).scalar_one_or_none():
            db.add(AccountRepo(account_id=acc.id, repo_name="hiero-bot-py"))

        await db.commit()

        # Create session
        _, cookie_val = await create_db_session(db, user.id)
        response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=cookie_val,
            httponly=True,
            max_age=SESSION_EXPIRE_SECONDS,
            samesite="lax",
            secure=False,
        )
        return response

    if not settings.github_oauth_client_id:
        log.warning("GITHUB_OAUTH_CLIENT_ID not set. OAuth login cannot proceed.")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="GitHub OAuth Client ID is not configured on this server.",
        )

    client = AsyncOAuth2Client(
        client_id=settings.github_oauth_client_id,
        client_secret=settings.github_oauth_client_secret,
        scope="read:org,user:email",
    )
    uri, state = client.create_authorization_url(AUTHORIZE_URL)

    response = RedirectResponse(url=uri)
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        max_age=600,
        samesite="lax",
        secure=settings.is_production,
    )
    return response


@router.get("/callback")
async def github_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    saved_state = request.cookies.get(STATE_COOKIE_NAME)
    if not state or not saved_state or state != saved_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state parameter",
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing code parameter",
        )

    client = AsyncOAuth2Client(
        client_id=settings.github_oauth_client_id,
        client_secret=settings.github_oauth_client_secret,
    )
    try:
        token_data = await client.fetch_token(
            ACCESS_TOKEN_URL,
            code=code,
            state=state,
        )
    except Exception as e:
        log.error("Failed to exchange OAuth code: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"GitHub OAuth error: {e}",
        )

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No access token returned from GitHub",
        )

    # Fetch GitHub User Profile
    user_res = await client.get(
        "https://api.github.com/user",
        headers={"User-Agent": "Hiero-Bot-Py"},
    )
    if user_res.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user profile from GitHub",
        )

    gh_user = user_res.json()
    github_user_id = gh_user["id"]
    github_login = gh_user["login"]
    github_email = gh_user.get("email")
    avatar_url = gh_user.get("avatar_url", "")
    encrypted_token = encrypt_token(access_token)
    now = datetime.now(timezone.utc)

    # Upsert user record
    stmt = select(User).where(User.github_user_id == github_user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        user.github_login = github_login
        user.github_email = github_email
        user.avatar_url = avatar_url
        user.last_login_at = now
    else:
        user = User(
            github_user_id=github_user_id,
            github_login=github_login,
            github_email=github_email,
            avatar_url=avatar_url,
            last_login_at=now,
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)

    # Upsert UserOAuthToken record
    token_stmt = select(UserOAuthToken).where(UserOAuthToken.user_id == user.id)
    token_res = await db.execute(token_stmt)
    user_token = token_res.scalar_one_or_none()

    if user_token:
        user_token.encrypted_access_token = encrypted_token
        user_token.scope = token_data.get("scope", "read:org")
        user_token.updated_at = now
    else:
        user_token = UserOAuthToken(
            user_id=user.id,
            encrypted_access_token=encrypted_token,
            scope=token_data.get("scope", "read:org"),
        )
        db.add(user_token)

    await db.commit()

    # Create server-side Session row in DB & sign cookie
    _, cookie_val = await create_db_session(db, user.id)

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(STATE_COOKIE_NAME)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_val,
        httponly=True,
        max_age=SESSION_EXPIRE_SECONDS,
        samesite="lax",
        secure=settings.is_production,
    )
    return response


@router.get("/logout")
@router.post("/logout")
async def github_oauth_logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        raw_session_id = unsign_session_id(cookie_val)
        if raw_session_id:
            await delete_db_session(db, raw_session_id)

    res = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    res.delete_cookie(SESSION_COOKIE_NAME)
    return res

