# app/scheduler/jobs.py — Scheduled background jobs

from __future__ import annotations

from dataclasses import dataclass, field

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config.loader import ConfigLoader
from app.db.database import AsyncSessionLocal
from app.github.client import GitHubClient
from app.utils.logger import get_logger
from app.workflows.issuemanagement import IssueManagementWorkflow

log = get_logger("scheduler")


@dataclass
class ScanSummary:
    """Aggregate outcome of one full stale scan across every installation."""

    installations: int = 0
    repos_scanned: int = 0
    repos_skipped: int = 0
    repos_failed: int = 0
    totals: dict[str, int] = field(
        default_factory=lambda: {"stale_marked": 0, "closed": 0, "unassigned": 0}
    )

    def add(self, counts: dict[str, int]) -> None:
        for key, value in counts.items():
            self.totals[key] = self.totals.get(key, 0) + value

    def as_dict(self) -> dict:
        return {
            "installations": self.installations,
            "repos_scanned": self.repos_scanned,
            "repos_skipped": self.repos_skipped,
            "repos_failed": self.repos_failed,
            **self.totals,
        }


class BotScheduler:
    def __init__(self, gh: GitHubClient, config_loader: ConfigLoader) -> None:
        self._gh = gh
        self._config_loader = config_loader
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        # Stale scan — every day at 02:00 UTC.
        # coalesce + max_instances=1 keep a slow scan from stacking up behind
        # itself if the process was paused or the previous run overran.
        self._scheduler.add_job(
            self.run_stale_scan,
            CronTrigger(hour=2, minute=0),
            id="stale_scan",
            name="Daily stale issue scan",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Config cache flush — every 6 hours
        self._scheduler.add_job(
            self._flush_config_cache,
            CronTrigger(hour="*/6"),
            id="config_cache_flush",
            name="Config cache flush",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()
        log.info("Scheduler started")

    def shutdown(self) -> None:
        try:
            if self._scheduler and getattr(self._scheduler, "running", False):
                self._scheduler.shutdown(wait=False)
        except Exception:
            pass

    async def run_stale_scan(self) -> ScanSummary:
        """
        Scan every repo of every installation for stale issues.

        Public so it can be triggered manually (and asserted on in tests) rather
        than only via the cron trigger.
        """
        log.info("Starting scheduled stale scan")
        summary = ScanSummary()

        try:
            installations = await self._gh.list_installations()
        except Exception as exc:
            log.error("Failed to list installations: %s", exc)
            return summary

        summary.installations = len(installations)

        for inst in installations:
            inst_id = inst.get("id")
            if not inst_id:
                log.warning("Installation entry without an id — skipping")
                continue

            try:
                repos = await self._gh.list_installation_repos(inst_id)
            except Exception as exc:
                log.error("Failed to list repos for installation %d: %s", inst_id, exc)
                continue

            for repo_data in repos:
                await self._scan_repo(inst_id, repo_data, summary)

        log.info("Stale scan complete: %s", summary.as_dict())
        return summary

    async def _scan_repo(
        self, inst_id: int, repo_data: dict, summary: ScanSummary
    ) -> None:
        full_name: str = repo_data.get("full_name", "")
        if "/" not in full_name:
            summary.repos_skipped += 1
            return

        owner, repo = full_name.split("/", 1)

        try:
            # The installation id is required: without it the loader falls back
            # to app-level auth, which cannot read a private repo's config and
            # 404s on it — so every scheduled scan silently found "no config"
            # and did nothing.
            config = await self._config_loader.load(owner, repo, inst_id)
            if not config or not config.workflows.issue_management.enabled:
                summary.repos_skipped += 1
                return

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

            summary.repos_scanned += 1
            summary.add(counts)
            log.info("Stale scan %s/%s: %s", owner, repo, counts)

        except Exception as exc:
            summary.repos_failed += 1
            log.error("Stale scan failed for %s/%s: %s", owner, repo, exc)

    async def _flush_config_cache(self) -> None:
        self._config_loader.clear()
        log.debug("Config cache flushed")
