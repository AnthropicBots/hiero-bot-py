# app/main.py — FastAPI application

from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
import pathlib

from app.db.database import init_db, get_db
from app.github.client import GitHubClient
from app.config.loader import ConfigLoader
from app.github.webhooks import WebhookRouter
from app.scheduler.jobs import BotScheduler
from app.api.routes import router as api_router
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

    log.info("Bot ready on port %d", settings.port)
    yield

    if _scheduler:
        _scheduler.shutdown()
    if _gh:
        await _gh.close()
    log.info("Bot shut down cleanly")


app = FastAPI(
    title="Hiero Maintainer Bot",
    description="Automated maintainer workflows for Hiero repositories",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(api_router)


# ── Webhook endpoint ──────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    assert _config_loader and _gh, "App not initialized"
    router = WebhookRouter(_gh, _config_loader)
    return await router.handle(request, db)


# ── Dashboard ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── Health ────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "2.0.0", "environment": settings.environment}
