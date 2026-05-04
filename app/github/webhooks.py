# app/github/webhooks.py — Webhook router

from __future__ import annotations
import hashlib
import hmac
from typing import Any
from fastapi import Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.config.loader import ConfigLoader
from app.github.client import GitHubClient
from app.workflows.onboarding import OnboardingWorkflow
from app.workflows.pullrequest import PullRequestWorkflow
from app.workflows.progression import ProgressionWorkflow
from app.workflows.issuemanagement import IssueManagementWorkflow
from app.workflows.prhealth import PRHealthWorkflow
from app.utils.settings import settings
from app.utils.logger import get_logger

log = get_logger("github.webhooks")

HELP_TEXT = """## 🤖 Hiero Bot Help

| Command | Description |
|---------|-------------|
| `/assign` | Self-assign this issue (eligibility checked) |
| `/unassign` | Remove yourself from this issue |
| `/check-eligibility` | View your role progression status |
| `/label <name>` | Add a label to this issue (maintainers only) |
| `/help` | Show this message |

_Powered by [hiero-maintainer-bot](https://github.com/hiero)_"""


class WebhookRouter:
    def __init__(self, gh: GitHubClient, config_loader: ConfigLoader) -> None:
        self._gh = gh
        self._config_loader = config_loader

    async def handle(self, request: Request, db: AsyncSession) -> dict:
        # Signature verification
        if settings.github_webhook_secret:
            self._verify_signature(request, await request.body())

        event = request.headers.get("X-GitHub-Event", "")
        payload: dict[str, Any] = await request.json()

        repo_data = payload.get("repository")
        installation_data = payload.get("installation")
        if not repo_data or not installation_data:
            return {"ok": True, "skipped": "no repo/installation"}

        owner = repo_data["owner"]["login"]
        repo = repo_data["name"]
        installation_id = installation_data["id"]

        config = await self._config_loader.load(owner, repo)
        if config is None:
            log.debug("No config for %s/%s — skipping", owner, repo)
            return {"ok": True, "skipped": "no config"}

        ctx = dict(
            owner=owner, repo=repo,
            installation_id=installation_id,
            config=config, db=db,
        )

        await self._dispatch(event, payload, ctx)
        return {"ok": True}

    async def _dispatch(
        self, event: str, payload: dict, ctx: dict
    ) -> None:
        action = payload.get("action", "")
        owner, repo = ctx["owner"], ctx["repo"]

        try:
            if event == "issues":
                await self._handle_issues(action, payload, ctx)

            elif event == "pull_request":
                await self._handle_pull_request(action, payload, ctx)

            elif event == "issue_comment":
                if action == "created":
                    body = payload.get("comment", {}).get("body", "")
                    if body.startswith("/"):
                        await self._handle_slash_command(body, payload, ctx)

            elif event == "push":
                # Invalidate config cache if bot config changed
                commits = payload.get("commits", [])
                if any(".github/hiero-bot.yml" in (c.get("modified") or [])
                       for c in commits):
                    self._config_loader.invalidate(owner, repo)
                    log.info("Config cache invalidated for %s/%s", owner, repo)

            else:
                log.debug("No handler for event=%s", event)

        except Exception as exc:
            log.error("Error in %s handler for %s/%s: %s", event, owner, repo, exc,
                      exc_info=True)

    #  Event handlers 

    async def _handle_issues(self, action: str, payload: dict, ctx: dict) -> None:
        wf_onboard = OnboardingWorkflow(self._gh)
        wf_issue = IssueManagementWorkflow(self._gh)

        if action == "opened":
            await wf_onboard.handle_new_contributor(ctx, payload)
        elif action == "labeled":
            await wf_issue.handle_label_escalation(ctx, payload)

    async def _handle_pull_request(self, action: str, payload: dict, ctx: dict) -> None:
        wf_pr = PullRequestWorkflow(self._gh)
        wf_health = PRHealthWorkflow(self._gh)
        wf_prog = ProgressionWorkflow(self._gh)

        pr = payload.get("pull_request", {})
        if pr.get("draft"):
            return

        if action in ("opened", "synchronize", "reopened"):
            await wf_pr.handle_pr_opened(ctx, payload)
            await wf_health.score_pr(ctx, payload)

        elif action == "closed" and pr.get("merged"):
            await wf_prog.handle_merged_pr(ctx, payload)

    #  Slash commands 

    async def _handle_slash_command(
        self, body: str, payload: dict, ctx: dict
    ) -> None:
        parts = body.strip().split()
        command = parts[0].lower()
        args = parts[1:]
        issue_number = (payload.get("issue") or {}).get("number")
        commenter = (payload.get("comment") or {}).get("user", {}).get("login")

        log.info("Slash command %s by @%s on #%s", command, commenter, issue_number)

        wf_onboard = OnboardingWorkflow(self._gh)
        wf_prog = ProgressionWorkflow(self._gh)
        gh = self._gh

        if command == "/assign":
            await wf_onboard.handle_self_assign(ctx, payload)

        elif command == "/unassign":
            if issue_number and commenter:
                await gh.remove_assignees(
                    ctx["owner"], ctx["repo"], issue_number,
                    [commenter], ctx["installation_id"]
                )
                await gh.post_comment(
                    ctx["owner"], ctx["repo"], issue_number,
                    f"✅ @{commenter} removed from this issue.",
                    ctx["installation_id"]
                )

        elif command == "/check-eligibility":
            await wf_prog.check_and_report(ctx, payload)

        elif command == "/help":
            if issue_number:
                await gh.post_comment(
                    ctx["owner"], ctx["repo"], issue_number,
                    HELP_TEXT, ctx["installation_id"]
                )

        elif command == "/label" and args:
            await self._handle_label_command(args[0], payload, ctx)

        else:
            log.debug("Unknown slash command: %s", command)

    async def _handle_label_command(
        self, label_name: str, payload: dict, ctx: dict
    ) -> None:
        issue_number = (payload.get("issue") or {}).get("number")
        commenter = (payload.get("comment") or {}).get("user", {}).get("login")
        if not issue_number or not commenter:
            return

        # Only maintainers/committers can label
        config = ctx["config"]
        org = ctx["owner"]
        inst = ctx["installation_id"]
        allowed = False
        for team in [config.teams.maintainers, config.teams.committers]:
            members = await self._gh.list_team_members(org, team, inst)
            if any(m["login"] == commenter for m in members):
                allowed = True
                break

        if not allowed:
            await self._gh.post_comment(
                ctx["owner"], ctx["repo"], issue_number,
                f"⛔ @{commenter} — only committers and maintainers can add labels via `/label`.",
                inst
            )
            return

        await self._gh.add_label(ctx["owner"], ctx["repo"], issue_number, label_name, inst)

    #  Signature verification 

    @staticmethod
    def _verify_signature(request: Request, body: bytes) -> None:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not sig_header:
            raise HTTPException(status_code=401, detail="Missing signature")

        expected = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=401, detail="Invalid signature")
