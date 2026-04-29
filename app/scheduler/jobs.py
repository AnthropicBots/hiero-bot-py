# app/scheduler/jobs.py — Scheduled background jobs

from __future__ import annotations
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.github.client import GitHubClient
from app.config.loader import ConfigLoader
from app.workflows.issuemanagement import IssueManagementWorkflow
from app.db.database import AsyncSessionLocal
from app.utils.logger import get_logger

log = get_logger("scheduler")


class BotScheduler:
    def __init__(self, gh: GitHubClient, config_loader: ConfigLoader) -> None:
        self._gh = gh
        self._config_loader = config_loader
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        # Stale scan — every day at 02:00 UTC
        self._scheduler.add_job(
            self._run_stale_scan,
            CronTrigger(hour=2, minute=0),
            id="stale_scan",
            name="Daily stale issue scan",
            replace_existing=True,
        )
        # Config cache flush — every 6 hours
        self._scheduler.add_job(
            self._flush_config_cache,
            CronTrigger(hour="*/6"),
            id="config_cache_flush",
            name="Config cache flush",
            replace_existing=True,
        )
        self._scheduler.start()
        log.info("Scheduler started")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def _run_stale_scan(self) -> None:
        log.info("Starting scheduled stale scan")
        try:
            installations = await self._gh.list_installations()
        except Exception as exc:
            log.error("Failed to list installations: %s", exc)
            return

        for inst in installations:
            inst_id = inst["id"]
            try:
                repos = await self._gh.list_installation_repos(inst_id)
            except Exception as exc:
                log.error("Failed to list repos for installation %d: %s", inst_id, exc)
                continue

            for repo_data in repos:
                full_name: str = repo_data.get("full_name", "")
                if "/" not in full_name:
                    continue
                owner, repo = full_name.split("/", 1)

                try:
                    config = await self._config_loader.load(owner, repo)
                    if not config or not config.workflows.issue_management.enabled:
                        continue

                    async with AsyncSessionLocal() as db:
                        ctx = {
                            "owner": owner,
                            "repo": repo,
                            "installation_id": inst_id,
                            "config": config,
                            "db": db,
                        }
                        wf = IssueManagementWorkflow(self._gh)
                        counts = await wf.run_stale_scan(ctx)
                        log.info("Stale scan %s/%s: %s", owner, repo, counts)

                except Exception as exc:
                    log.error("Stale scan failed for %s/%s: %s", owner, repo, exc)

        log.info("Stale scan complete")

    async def _flush_config_cache(self) -> None:
        self._config_loader.clear()
        log.debug("Config cache flushed")
