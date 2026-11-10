# app/github/webhooks.py — Webhook router

from __future__ import annotations

import csv
import difflib
import hashlib
import hmac
import time  # #5
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.gating import is_premium
from app.config.loader import ConfigInvalid, ConfigLoader
from app.db.models import Account, AccountRepo
from app.github.client import GitHubClient
from app.github.replay_guard import is_replay, release_delivery
from app.utils.logger import get_logger
from app.utils.settings import settings
from app.workflows.issuemanagement import IssueManagementWorkflow
from app.workflows.onboarding import OnboardingWorkflow
from app.workflows.prhealth import PRHealthWorkflow
from app.workflows.progression import ProgressionWorkflow
from app.workflows.pullrequest import PullRequestWorkflow
from app.workflows.reviewerassignment import ReviewerAssignmentWorkflow

log = get_logger("github.webhooks")

HELP_TEXT = """## 🤖 Hiero Bot Help

| Command | Description |
|---------|-------------|
| `/assign` | Self-assign this issue (eligibility checked) |
| `/unassign` | Remove yourself from this issue |
| `/check-eligibility` | View your role progression status |
| `/label <name>` | Add a label to this issue (maintainers only) |
| `/help` | Show this message |

_Powered by [hiero-maintainer-bot](https://github.com/AnthropicBots)_"""


def _parse_label_names(raw: str) -> list[str]:
    """
    Parse label names from a /label command string.
    Commas separate multiple labels.
    Quotes are optional around label names.
    """
    if not raw or not raw.strip():
        return []
    try:
        reader = csv.reader([raw], skipinitialspace=True)
        items = next(reader, [])
    except Exception:
        items = raw.split(",")

    labels = []
    for item in items:
        cleaned = item.strip()
        if (cleaned.startswith('"') and cleaned.endswith('"')) or (
            cleaned.startswith("'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[1:-1].strip()
        if cleaned:
            labels.append(cleaned)
    return labels


class WebhookRouter:
    def __init__(self, gh: GitHubClient, config_loader: ConfigLoader) -> None:
        self._gh = gh
        self._config_loader = config_loader

    async def handle(self, request: Request, db: AsyncSession) -> dict:
        # Signature verification (supports current & rotated secret)
        if settings.github_webhook_secret or settings.github_webhook_secret_old or settings.is_production:
            self._verify_signature(request, await request.body())

        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        if delivery_id and await is_replay(db, delivery_id):
            log.warning("Rejected replayed delivery %s", delivery_id)
            raise HTTPException(status_code=409, detail="Duplicate delivery")

        # #5 — skew check, defense-in-depth (secondary to signature + replay ID)
        date_header = request.headers.get("Date")
        if date_header:
            try:
                from email.utils import parsedate_to_datetime

                sent_at = parsedate_to_datetime(date_header).timestamp()
                skew = abs(time.time() - sent_at)
                if skew > settings.webhook_max_skew_seconds:
                    log.warning(
                        "Webhook delivery %s exceeded max skew (%.0fs)",
                        delivery_id,
                        skew,
                    )
                    raise HTTPException(
                        status_code=401, detail="Delivery timestamp out of range"
                    )
            except (TypeError, ValueError):
                pass  # unparseable Date header — skip, don't fail the request

        event = request.headers.get("X-GitHub-Event", "")
        payload: dict[str, Any] = await request.json()

        # Handle installation lifecycle events (which do not require repository payload)
        if event == "installation":
            return await self._handle_installation(payload, db)
        elif event == "installation_repositories":
            try:
                return await self._handle_installation_repositories(payload, db)
            except HTTPException as exc:
                if exc.status_code == 409:
                    try:
                        await release_delivery(db, delivery_id)
                    except Exception:
                        log.exception(
                            "Failed to release webhook delivery %s after retryable error",
                            delivery_id,
                        )
                raise

        repo_data = payload.get("repository")
        installation_data = payload.get("installation")
        if not repo_data or not installation_data:
            return {"ok": True, "skipped": "no repo/installation"}

        owner = repo_data["owner"]["login"]
        repo = repo_data["name"]
        installation_id = installation_data["id"]

        # Invalidate config cache before loading so config changes are
        # discovered even when a negative-cache entry exists.
        if event == "push":
            commits = payload.get("commits", [])
            config_changed = any(
                ".github/hiero-bot.yml"
                in (
                    commit.get("modified", [])
                    + commit.get("added", [])
                    + commit.get("removed", [])
                )
                for commit in commits
            )
            if config_changed:
                self._config_loader.invalidate(owner, repo)
                log.info("Config cache invalidated for %s/%s", owner, repo)

        try:
            config = await self._config_loader.load(owner, repo, installation_id)
        except ConfigInvalid:
            # ConfigLoader logs the invalid config when it is first cached.
            # Avoid repeating an ERROR for every webhook delivery that
            # encounters the cached invalid configuration.
            return {"ok": True, "skipped": "invalid config"}

        if config is None:
            log.debug("No config for %s/%s — skipping", owner, repo)
            return {"ok": True, "skipped": "no config"}

        # Look up the Account so downstream handlers can check plan_tier.
        account = await db.scalar(
            select(Account).where(
                Account.github_installation_id == installation_id
            )
        )

        ctx = {
            "owner": owner,
            "repo": repo,
            "installation_id": installation_id,
            "config": config,
            "db": db,
            "account": account,
        }

        await self._dispatch(event, payload, ctx)
        return {"ok": True}

    async def _dispatch(self, event: str, payload: dict, ctx: dict) -> None:
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
                log.debug("Processed push event for %s/%s", owner, repo)

            else:
                log.debug("No handler for event=%s", event)

        except Exception:
            log.exception(
                "Error in %s handler for %s/%s",
                event,
                owner,
                repo,
            )

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
        wf_reviewer = ReviewerAssignmentWorkflow(self._gh)
        wf_health = PRHealthWorkflow(self._gh)
        wf_prog = ProgressionWorkflow(self._gh)

        pr = payload.get("pull_request", {})
        if not pr:
            return

        if action == "opened" and pr.get("draft"):
            return

        account = ctx.get("account")
        premium = account is not None and is_premium(account)

        if action in ("opened", "ready_for_review", "synchronize", "reopened"):
            # Each step is isolated: one failing (e.g. GitHub rejecting a reviewer
            # request with 422) must not silently skip the steps after it.
            await self._run_step(
                "quality gates", wf_pr.handle_pr_opened(ctx, payload, action), ctx
            )

            # Reviewer assignment is a Premium feature (AI-powered rota).
            if action in ("opened", "ready_for_review"):
                if premium:
                    await self._run_step(
                        "reviewer assignment", wf_reviewer.handle_pr_opened(ctx, payload), ctx
                    )
                else:
                    log.debug(
                        "Skipping reviewer assignment for %s/%s — account is not premium",
                        ctx.get("owner"),
                        ctx.get("repo"),
                    )

            # PR health scoring is available to all accounts (free feature).
            await self._run_step("health scoring", wf_health.score_pr(ctx, payload), ctx)

        elif action == "closed" and pr.get("merged"):
            await wf_prog.handle_merged_pr(ctx, payload)

    @staticmethod
    async def _run_step(name: str, step, ctx: dict) -> None:
        try:
            await step
        except Exception:
            log.exception(
                "PR step '%s' failed for %s/%s; continuing with remaining steps",
                name,
                ctx.get("owner"),
                ctx.get("repo"),
            )

    #  Slash commands

    async def _handle_slash_command(self, body: str, payload: dict, ctx: dict) -> None:
        parts = body.strip().split()
        command = parts[0].lower()
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
                    ctx["owner"],
                    ctx["repo"],
                    issue_number,
                    [commenter],
                    ctx["installation_id"],
                )
                await gh.post_comment(
                    ctx["owner"],
                    ctx["repo"],
                    issue_number,
                    f"✅ @{commenter} removed from this issue.",
                    ctx["installation_id"],
                )

        elif command == "/check-eligibility":
            await wf_prog.check_and_report(ctx, payload)

        elif command == "/help":
            if issue_number:
                await gh.post_comment(
                    ctx["owner"],
                    ctx["repo"],
                    issue_number,
                    HELP_TEXT,
                    ctx["installation_id"],
                )

        elif command == "/label":
            raw_args = body.strip()[len(parts[0]):].strip()
            if raw_args:
                await self._handle_label_command(raw_args, payload, ctx)

        else:
            log.debug("Unknown slash command: %s", command)

    async def _handle_label_command(
        self, raw_labels: str, payload: dict, ctx: dict
    ) -> None:
        issue = payload.get("issue") or {}
        issue_number = issue.get("number")
        commenter = (payload.get("comment") or {}).get("user", {}).get("login")

        if not issue_number or not commenter:
            return

        inst = ctx["installation_id"]

        permission = await self._gh.get_collaborator_permission(
            ctx["owner"],
            ctx["repo"],
            commenter,
            inst,
        )
        allowed = permission in {"admin", "maintain", "write"}

        if not allowed:
            await self._gh.post_comment(
                ctx["owner"],
                ctx["repo"],
                issue_number,
                f"⛔ @{commenter} — only committers and maintainers can add labels via `/label`.",
                inst,
            )
            return

        requested_labels = _parse_label_names(raw_labels)
        if not requested_labels:
            return

        repo_labels = await self._gh.list_labels(ctx["owner"], ctx["repo"], inst)
        existing_names = [
            lbl["name"] if isinstance(lbl, dict) else str(lbl)
            for lbl in (repo_labels or [])
        ]
        canonical_map = {name.lower(): name for name in existing_names}

        valid_labels: list[str] = []
        unknown_labels: list[str] = []

        for req in requested_labels:
            lower_req = req.lower()
            if lower_req in canonical_map:
                canonical_name = canonical_map[lower_req]
                if canonical_name not in valid_labels:
                    valid_labels.append(canonical_name)
            else:
                if req not in unknown_labels:
                    unknown_labels.append(req)

        for label in valid_labels:
            await self._gh.add_label(
                ctx["owner"],
                ctx["repo"],
                issue_number,
                label,
                inst,
                create_if_missing=False,
            )

        if unknown_labels:
            if len(unknown_labels) == 1:
                unk = unknown_labels[0]
                matches = difflib.get_close_matches(unk, existing_names, n=3, cutoff=0.6)
                if matches:
                    suggestions = ", ".join(f"`{m}`" for m in matches)
                    msg = f"⚠️ Label `{unk}` does not exist in this repository. Did you mean: {suggestions}?"
                else:
                    msg = f"⚠️ Label `{unk}` does not exist in this repository."
            else:
                lines = ["⚠️ The following label(s) do not exist in this repository:"]
                for unk in unknown_labels:
                    matches = difflib.get_close_matches(unk, existing_names, n=3, cutoff=0.6)
                    if matches:
                        suggestions = ", ".join(f"`{m}`" for m in matches)
                        lines.append(f"- `{unk}` (did you mean: {suggestions}?)")
                    else:
                        lines.append(f"- `{unk}`")
                msg = "\n".join(lines)

            await self._gh.post_comment(
                ctx["owner"],
                ctx["repo"],
                issue_number,
                msg,
                inst,
            )

    #  Signature verification

    @staticmethod
    def _verify_signature(request: Request, body: bytes) -> None:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not sig_header:
            raise HTTPException(status_code=401, detail="Missing signature")

        secrets = [s for s in (settings.github_webhook_secret, settings.github_webhook_secret_old) if s]

        for secret in secrets:
            expected = (
                "sha256="
                + hmac.new(
                    secret.encode("utf-8"),
                    body,
                    hashlib.sha256,
                ).hexdigest()
            )
            if hmac.compare_digest(
                sig_header.encode("utf-8"), expected.encode("utf-8")
            ):
                return

        raise HTTPException(status_code=401, detail="Invalid signature")

    # ── Installation Webhook Handlers ──────────────────────────

    @staticmethod
    def _installation_insert(table: Any, dialect: str) -> Any:
        if dialect == "sqlite":
            return sqlite_insert(table)

        if dialect == "postgresql":
            return postgresql_insert(table)

        raise RuntimeError(
            f"Unsupported database dialect for installation upsert: {dialect}"
        )

    @staticmethod
    def _installation_transaction(db: AsyncSession) -> Any:
        """Return a transaction context appropriate for the current session."""
        if db.in_transaction():
            return db.begin_nested()

        return db.begin()

    async def _handle_installation(self, payload: dict, db: AsyncSession) -> dict:
        action = payload.get("action", "")
        inst = payload.get("installation")

        if not isinstance(inst, dict):
            raise HTTPException(
                status_code=400,
                detail="Invalid installation payload: installation object is missing",
            )

        inst_id = inst.get("id")

        if inst_id is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid installation payload: installation ID is missing",
            )

        if isinstance(inst_id, bool) or not isinstance(inst_id, int) or inst_id <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid installation payload: installation ID must be a positive integer",
            )

        if action in ("created", "unsuspended"):
            account_info = inst.get("account")

            if not isinstance(account_info, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid installation payload: account information is missing",
                )

            org_login = account_info.get("login")
            github_account_id = account_info.get("id")
            account_type = account_info.get("type", "Organization")

            if (
                not isinstance(org_login, str)
                or not org_login.strip()
                or isinstance(github_account_id, bool)
                or not isinstance(github_account_id, int)
                or github_account_id <= 0
                or not isinstance(account_type, str)
                or not account_type.strip()
            ):
                log.warning(
                    "Installation %s has invalid account information",
                    inst_id,
                )
                raise HTTPException(
                    status_code=400,
                    detail="Invalid installation payload: account information is invalid",
                )

            repos = payload.get("repositories", [])

            if not isinstance(repos, list):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid installation payload: repositories must be a list",
                )

            repo_names = set()

            for repo_data in repos:
                if not isinstance(repo_data, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid installation payload: repository entry must be an object",
                    )

                repo_name = repo_data.get("name")

                if not isinstance(repo_name, str):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid installation payload: repository name must be a string",
                    )

                normalized_name = repo_name.strip()

                if not normalized_name or len(normalized_name) > 128:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid installation payload: repository name is empty or too long",
                    )

                repo_names.add(normalized_name)

            dialect = db.get_bind().dialect.name

            account_values = {
                "github_installation_id": inst_id,
                "github_account_id": github_account_id,
                "org_login": org_login.strip(),
                "account_type": account_type.strip(),
                "plan_tier": "free",
                "suspended_at": None,
            }

            account_update = {
                "github_account_id": github_account_id,
                "org_login": org_login.strip(),
                "account_type": account_type.strip(),
                "suspended_at": None,
            }

            account_stmt = (
                self._installation_insert(Account, dialect)
                .values(**account_values)
                .on_conflict_do_update(
                    index_elements=[Account.github_installation_id],
                    set_=account_update,
                )
            )

            async with self._installation_transaction(db):
                await db.execute(account_stmt)

                acc = await db.scalar(
                    select(Account)
                    .where(Account.github_installation_id == inst_id)
                    .execution_options(populate_existing=True)
                )

                if acc is None:
                    raise RuntimeError(
                        f"Installation account {inst_id} was not available after upsert"
                    )

                if repo_names:
                    repo_values = [
                        {
                            "account_id": acc.id,
                            "repo_name": repo_name,
                        }
                        for repo_name in sorted(repo_names)
                    ]

                    repo_stmt = (
                        self._installation_insert(AccountRepo, dialect)
                        .values(repo_values)
                        .on_conflict_do_nothing(
                            index_elements=[
                                AccountRepo.account_id,
                                AccountRepo.repo_name,
                            ]
                        )
                    )

                    await db.execute(repo_stmt)

            log.info(
                "Installation %s (%s) upserted with %d repos",
                inst_id,
                org_login,
                len(repo_names),
            )

        elif action in ("deleted", "suspend"):
            async with self._installation_transaction(db):
                acc = await db.scalar(
                    select(Account).where(
                        Account.github_installation_id == inst_id
                    )
                )

                if acc:
                    acc.suspended_at = datetime.now(UTC)

        return {"ok": True, "action": action}

    async def _handle_installation_repositories(
        self,
        payload: dict,
        db: AsyncSession,
    ) -> dict:
        action = payload.get("action", "")
        installation = payload.get("installation")

        if not isinstance(installation, dict):
            raise HTTPException(
                status_code=400,
                detail="Invalid repository event: installation object is missing",
            )

        inst_id = installation.get("id")

        if inst_id is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid repository event: installation ID is missing",
            )

        if isinstance(inst_id, bool) or not isinstance(inst_id, int) or inst_id <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid repository event: installation ID must be a positive integer",
            )

        if action not in ("added", "removed"):
            return {"ok": True, "action": action}

        if action == "added":
            repositories = payload.get("repositories_added")
        else:
            repositories = payload.get("repositories_removed")

        if not isinstance(repositories, list):
            raise HTTPException(
                status_code=400,
                detail="Invalid repository event: repositories must be a list",
            )

        repo_names = set()

        for repo_data in repositories:
            if not isinstance(repo_data, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid repository event: repository entry must be an object",
                )

            repo_name = repo_data.get("name")

            if not isinstance(repo_name, str):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid repository event: repository name must be a string",
                )

            normalized_name = repo_name.strip()

            if not normalized_name or len(normalized_name) > 128:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid repository event: repository name is empty or too long",
                )

            repo_names.add(normalized_name)

        async with self._installation_transaction(db):
            acc = await db.scalar(
                select(Account).where(
                    Account.github_installation_id == inst_id
                )
            )

            if acc is None:
                account_info = installation.get("account")

                if not isinstance(account_info, dict):
                    log.warning(
                        "Installation %s not found and repository event has no account information",
                        inst_id,
                    )
                    raise HTTPException(
                        status_code=409,
                        detail="Installation account not found; retry delivery",
                    )

                github_account_id = account_info.get("id")
                org_login = account_info.get("login")
                account_type = account_info.get("type", "Organization")

                if (
                    isinstance(github_account_id, bool)
                    or not isinstance(github_account_id, int)
                    or github_account_id <= 0
                    or not isinstance(org_login, str)
                    or not org_login.strip()
                    or not isinstance(account_type, str)
                    or not account_type.strip()
                ):
                    log.warning(
                        "Installation %s has invalid account information in repository event",
                        inst_id,
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid repository event: installation account information is invalid",
                    )

                dialect = db.get_bind().dialect.name

                account_stmt = (
                    self._installation_insert(Account, dialect)
                    .values(
                        github_installation_id=inst_id,
                        github_account_id=github_account_id,
                        org_login=org_login.strip(),
                        account_type=account_type.strip(),
                        plan_tier="free",
                        suspended_at=None,
                    )
                    .on_conflict_do_update(
                        index_elements=[Account.github_installation_id],
                        set_={
                            "github_account_id": github_account_id,
                            "org_login": org_login.strip(),
                            "account_type": account_type.strip(),
                            "suspended_at": None,
                        },
                    )
                )

                await db.execute(account_stmt)

                acc = await db.scalar(
                    select(Account)
                    .where(Account.github_installation_id == inst_id)
                    .execution_options(populate_existing=True)
                )

                if acc is None:
                    raise RuntimeError(
                        f"Installation account {inst_id} was not available after upsert"
                    )

                log.info(
                    "Recovered missing installation account %s (%s) from repository event",
                    inst_id,
                    org_login,
                )

            if action == "added" and repo_names:
                dialect = db.get_bind().dialect.name

                repo_values = [
                    {
                        "account_id": acc.id,
                        "repo_name": repo_name,
                    }
                    for repo_name in sorted(repo_names)
                ]

                repo_stmt = (
                    self._installation_insert(AccountRepo, dialect)
                    .values(repo_values)
                    .on_conflict_do_nothing(
                        index_elements=[
                            AccountRepo.account_id,
                            AccountRepo.repo_name,
                        ]
                    )
                )

                await db.execute(repo_stmt)

            elif action == "removed" and repo_names:
                await db.execute(
                    delete(AccountRepo).where(
                        AccountRepo.account_id == acc.id,
                        AccountRepo.repo_name.in_(repo_names),
                    )
                )

        log.info(
            "%s %d repos for installation %s",
            "Added" if action == "added" else "Removed",
            len(repo_names),
            inst_id,
        )

        return {"ok": True, "action": action}
