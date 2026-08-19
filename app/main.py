# app/main.py — FastAPI application

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import router as api_router
from app.config.loader import ConfigLoader
from app.db.database import get_db, init_db
from app.github.client import GitHubClient
from app.github.webhooks import WebhookRouter
from app.scheduler.jobs import BotScheduler
from app.utils.auth import _auth_configured
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("main")

# Singletons
_gh: GitHubClient | None = None
_config_loader: ConfigLoader | None = None
_scheduler: BotScheduler | None = None

TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "dashboard" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gh, _config_loader, _scheduler

    log.info("Starting Hiero Maintainer Bot (Python)")
    await init_db()

    _gh = GitHubClient()
    _config_loader = ConfigLoader(_gh)
    _scheduler = BotScheduler(_gh, _config_loader)

    if settings.is_production:
        _scheduler.start()
        if not _auth_configured():
            log.warning(
                "DASHBOARD_USERNAME/DASHBOARD_PASSWORD are unset in production -"
                "dashboard and /api/v1/* are UNAUTHENTICATED."
            )

    log.info("Bot ready on port %d", settings.port)
    yield

    if _scheduler:
        _scheduler.shutdown()
    if _gh:
        await _gh.close()
    log.info("Bot shut down cleanly")


from app.auth.oauth import router as auth_router
from app.billing.stripe_webhooks import router as stripe_router

app = FastAPI(
    title="Hiero Maintainer Bot",
    description="Automated maintainer workflows for Hiero repositories",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins if settings.is_production else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled server exception on %s %s: %s", request.method, request.url.path, exc, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred."},
    )


from app.auth.dependencies import get_current_user_optional
from app.db.models import User

app.include_router(auth_router)
app.include_router(stripe_router)
app.include_router(api_router)


# ── Webhook endpoint ──────────────────────────────────────────


@app.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    assert _config_loader and _gh, "App not initialized"
    router = WebhookRouter(_gh, _config_loader)
    return await router.handle(request, db)


# ── Dashboard ─────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
):
    if not user:
        return templates.TemplateResponse(request, "login.html")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user},
    )


# ── Health Check (Deep) ───────────────────────────────────────


@app.get("/healthz")
async def healthz(db: AsyncSession = Depends(get_db)):
    db_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        log.error("Database health check failed: %s", e)
        db_status = "error"

    status_code = 200 if db_status == "ok" else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if db_status == "ok" else "degraded",
            "database": db_status,
            "version": "2.0.0",
            "environment": settings.environment,
        },
    )

