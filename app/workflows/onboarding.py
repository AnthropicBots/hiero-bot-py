# app/workflows/onboarding.py

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import AuditLog
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.onboarding")

_assign_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
_contributor_assign_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def _get_assign_lock(owner: str, repo: str, issue_number: int) -> asyncio.Lock:
    key = (owner, repo, issue_number)
    if key not in _assign_locks:
        _assign_locks[key] = asyncio.Lock()
    return _assign_locks[key]


def _get_contributor_assign_lock(
    owner: str,
    repo: str,
    login: str,
) -> asyncio.Lock:
    key = (owner, repo, login.lower())
    if key not in _contributor_assign_locks:
        _contributor_assign_locks[key] = asyncio.Lock()
    return _contributor_assign_locks[key]


# Bot detection. Substring matching is deliberately avoided — "bot" appears in
# plenty of human logins (robotics-sam, abbot, talbot), and silently skipping
# those people is worse than occasionally welcoming a bot.
BOT_LOGIN_SUFFIXES = ("[bot]",)
BOT_LOGIN_EXACT = frozenset(
    {
        "dependabot",
        "renovate",
        "renovate-bot",
        "github-actions",
        "codecov",
        "codecov-io",
        "greenkeeper",
        "snyk-bot",
        "imgbot",
        "allcontributors",
        "mergify",
        "stale",
        "semantic-release-bot",
        "pre-commit-ci",
    }
)

# GitHub reports how the author relates to the repository. Anyone in this set
# has demonstrably interacted with the project before, so they are not new.
ESTABLISHED_ASSOCIATIONS = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
)


def looks_like_bot(login: str, account_type: str = "") -> bool:
    """True when the account is a bot by GitHub's own reckoning or by login shape."""
    if account_type == "Bot":
        return True

    normalized = login.lower()
    if any(normalized.endswith(suffix) for suffix in BOT_LOGIN_SUFFIXES):
        return True

    # Strip a trailing "-bot"/"bot" style qualifier before the exact match so
    # both "renovate" and "renovate-bot" resolve to a known automation account.
    return normalized in BOT_LOGIN_EXACT


class OnboardingWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    async def handle_new_contributor(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.onboarding
        if not cfg.enabled:
            return

        sender = payload.get("sender", {})
        login: str = sender.get("login", "")
        issue = payload.get("issue") or {}
        issue_number: int | None = issue.get("number")
        if not login or not issue_number:
            return

        # `check_human_contributors: false` still respects GitHub's own account
        # type — it only turns off the heuristic login matching on top of it.
        if sender.get("type") == "Bot" or (
            cfg.check_human_contributors and looks_like_bot(login)
        ):
            log.debug("Skipping bot account: %s", login)
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db = ctx["db"]

        if cfg.welcome_first_time_only:
            first_time = await self._is_first_time(ctx, login, issue_number, issue)
            if not first_time:
                log.debug(
                    "@%s is an established contributor in %s/%s — no welcome",
                    login,
                    owner,
                    repo,
                )
                return

        msg = self._build_welcome(login, cfg)
        await self._gh.post_comment(owner, repo, issue_number, msg, inst)

        await audit.record(
            db,
            action="contributor.welcomed",
            owner=owner,
            repo=repo,
            target_number=issue_number,
            target_login=login,
            reason="First-time contributor",
        )
        await db.commit()

        # Assign mentor
        if cfg.auto_assign_mentor and ctx["config"].teams.mentors:
            await self._assign_mentor(ctx, issue_number, login)

    async def handle_self_assign(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.onboarding
        if not cfg.enabled:
            return

        login: str = (payload.get("comment") or {}).get("user", {}).get("login", "")
        issue = payload.get("issue") or {}
        issue_number: int | None = issue.get("number")
        if not login or not issue_number:
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db = ctx["db"]

        async with _get_assign_lock(owner, repo, issue_number):
            live_issue = await self._gh.get(
                f"/repos/{owner}/{repo}/issues/{issue_number}", inst
            )
            assignees = [a["login"] for a in (live_issue.get("assignees") or [])]

            if assignees:
                if login in assignees:
                    msg = f"@{login} You're already assigned! 🎉"
                else:
                    msg = f"@{login} This issue is already assigned to @{assignees[0]}. Try another one!"
                await self._gh.post_comment(owner, repo, issue_number, msg, inst)
                return

            if cfg.max_concurrent_assignments is not None:
                async with _get_contributor_assign_lock(owner, repo, login):
                    ok, reason = await self._check_eligibility(ctx, login)
                    if not ok:
                        await self._gh.post_comment(
                            owner, repo, issue_number, reason, inst
                        )
                        return

                    await self._gh.add_assignees(
                        owner, repo, issue_number, [login], inst
                    )
            else:
                ok, reason = await self._check_eligibility(ctx, login)
                if not ok:
                    await self._gh.post_comment(
                        owner, repo, issue_number, reason, inst
                    )
                    return

                await self._gh.add_assignees(
                    owner, repo, issue_number, [login], inst
                )

            await self._gh.post_comment(
                owner,
                repo,
                issue_number,
                f"✅ @{login} has been assigned! Good luck — ask questions any time.",
                inst,
            )
            await audit.record(
                db,
                action="issue.assigned",
                owner=owner,
                repo=repo,
                target_number=issue_number,
                target_login=login,
                reason="Self-assignment via /assign",
            )
            await db.commit()

    # ── First-time detection ──────────────────────────────────

    async def _is_first_time(
        self, ctx: dict, login: str, issue_number: int, issue: dict
    ) -> bool:
        """
        Decide whether this is the contributor's first interaction with the repo.

        Three independent signals, cheapest first:

        1. `author_association` — GitHub already tells us when the author is an
           owner, member, collaborator or past contributor.
        2. The audit trail — if we welcomed them before, don't do it again, even
           if GitHub's view of them has since changed.
        3. Their issue/PR history in this repo — the only signal that catches
           someone who has opened issues but never had a PR merged, which is
           exactly the case the old code got wrong.
        """
        association = (issue.get("author_association") or "").upper()
        if association in ESTABLISHED_ASSOCIATIONS:
            return False

        if await self._already_welcomed(ctx, login):
            return False

        return await self._has_no_prior_issues(ctx, login, issue_number)

    async def _already_welcomed(self, ctx: dict, login: str) -> bool:
        try:
            result = await ctx["db"].execute(
                select(AuditLog.id)
                .where(
                    AuditLog.owner == ctx["owner"],
                    AuditLog.repo == ctx["repo"],
                    AuditLog.target_login == login,
                    AuditLog.action == "contributor.welcomed",
                )
                .limit(1)
            )
            return result.scalar() is not None
        except Exception as exc:
            log.warning("Could not check welcome history for @%s: %s", login, exc)
            return False

    async def _has_no_prior_issues(
        self, ctx: dict, login: str, issue_number: int
    ) -> bool:
        """True when the issue being handled is the only one this login has opened."""
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        try:
            created = await self._gh.get(
                f"/repos/{owner}/{repo}/issues",
                inst,
                params={
                    "creator": login,
                    "state": "all",
                    "per_page": 5,
                    "sort": "created",
                    "direction": "asc",
                },
            )
        except Exception as exc:
            # If contributor history cannot be verified, do not claim that this
            # is a first-time contributor.
            log.warning("Could not read issue history for @%s: %s", login, exc)
            return False

        others = [
            item
            for item in (created or [])
            if item.get("number") != issue_number
        ]
        return not others

    # ── Eligibility ───────────────────────────────────────────

    async def _check_eligibility(self, ctx: dict, login: str) -> tuple[bool, str]:
        cfg = ctx["config"].workflows.onboarding
        if cfg.max_concurrent_assignments is not None:
            owner = ctx["owner"]
            repo = ctx["repo"]
            installation_id = ctx["installation_id"]

            try:
                current = await self._gh.count_assigned_open_issues(
                    owner,
                    repo,
                    login,
                    installation_id,
                )
            except Exception as exc:
                log.error(
                    "Could not check concurrent assignments for @%s in %s/%s: %s",
                    login,
                    owner,
                    repo,
                    exc,
                )
                return (
                    False,
                    (
                        f"⚠️ @{login} We couldn't verify your current issue "
                        "assignments. Please try again later."
                    ),
                )

            if current >= cfg.max_concurrent_assignments:
                return (
                    False,
                    (
                        f"⚠️ @{login} You already have {current} open issue(s) "
                        "assigned in this repository. Please finish one before "
                        "taking another issue."
                    ),
                )

        # Account-quality checks fail open: a GitHub hiccup should not stop a
        # legitimate contributor from picking up an issue.
        try:
            user = await self._gh.get_user(login, ctx["installation_id"])
            created = datetime.fromisoformat(user["created_at"].replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days

            if age_days < cfg.minimum_account_age_days:
                return False, (
                    f"⚠️ @{login} Your account must be at least "
                    f"**{cfg.minimum_account_age_days} days old** to self-assign "
                    f"(current: {age_days} days)."
                )
            if (user.get("public_repos") or 0) < cfg.minimum_public_contributions:
                return False, (
                    f"⚠️ @{login} Your account needs at least "
                    f"**{cfg.minimum_public_contributions} public repos** to qualify."
                )
        except Exception as exc:
            log.warning("Account checks skipped for @%s: %s", login, exc)

        # The CLA gate fails closed — "required" has to mean required.
        if cfg.require_signed_cla and not await self._has_signed_cla(ctx, login):
            return False, self._cla_notice(login, cfg)

        return True, ""

    async def _has_signed_cla(self, ctx: dict, login: str) -> bool:
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        path = ctx["config"].workflows.onboarding.cla_signatures_file

        try:
            raw_b64 = await self._gh.get_file_content(owner, repo, path, inst)
        except Exception as exc:
            log.error("CLA signature file %s unreadable: %s", path, exc)
            return False

        if not raw_b64:
            log.warning(
                "require_signed_cla is on but %s is missing in %s/%s", path, owner, repo
            )
            return False

        try:
            document = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            log.error("CLA signature file %s is not valid JSON: %s", path, exc)
            return False

        return login.lower() in self._signatory_logins(document)

    @staticmethod
    def _signatory_logins(document: object) -> set[str]:
        """
        Extract signatory logins from the common CLA-file shapes.

        Supports cla-assistant's `{"signedContributors": [{"name": "..."}]}`, a
        bare list of logins, and a list of `{"login": "..."}` objects.
        """
        if isinstance(document, dict):
            entries = document.get("signedContributors") or document.get("signatures")
        else:
            entries = document

        if not isinstance(entries, list):
            return set()

        logins: set[str] = set()
        for entry in entries:
            if isinstance(entry, str):
                logins.add(entry.lower())
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("login") or entry.get("username")
                if isinstance(name, str):
                    logins.add(name.lower())
        return logins

    @staticmethod
    def _cla_notice(login: str, cfg) -> str:
        link = (
            f"\n\nSign it here: {cfg.cla_document_url}" if cfg.cla_document_url else ""
        )
        return (
            f"⚠️ @{login} This repository requires a signed Contributor License "
            f"Agreement before you can be assigned an issue.{link}\n\n"
            f"Once your signature appears in `{cfg.cla_signatures_file}`, comment "
            f"`/assign` again."
        )

    # ── Mentors ───────────────────────────────────────────────

    async def _assign_mentor(
        self, ctx: dict, issue_number: int, contributor: str
    ) -> None:
        org = ctx["owner"]
        team_slug = ctx["config"].teams.mentors
        inst = ctx["installation_id"]
        db = ctx["db"]

        members = await self._gh.list_team_members(org, team_slug, inst)
        if not members:
            return

        strategy = ctx["config"].workflows.onboarding.mentor_assignment_strategy
        if strategy == "round-robin":
            idx = sum(ord(c) for c in contributor) % len(members)
            mentor = members[idx]["login"]
        else:
            mentor = members[0]["login"]

        await self._gh.add_assignees(
            ctx["owner"], ctx["repo"], issue_number, [mentor], inst
        )
        await self._gh.post_comment(
            ctx["owner"],
            ctx["repo"],
            issue_number,
            f"👋 @{mentor} has been assigned as mentor to support @{contributor}.",
            inst,
        )
        await audit.record(
            db,
            action="contributor.mentor_assigned",
            owner=ctx["owner"],
            repo=ctx["repo"],
            target_number=issue_number,
            target_login=contributor,
            reason=f"Mentor @{mentor} assigned via {strategy}",
            metadata={"mentor": mentor},
        )
        await db.commit()

    @staticmethod
    def _build_welcome(login: str, cfg) -> str:
        checklist = ""
        if cfg.onboarding_checklist:
            items = "\n".join(f"- [ ] {item}" for item in cfg.onboarding_checklist)
            checklist = f"\n\n**Getting Started Checklist:**\n{items}"

        custom = f"\n\n{cfg.welcome_message}" if cfg.welcome_message else ""

        return f"""## 👋 Welcome to Hiero, @{login}!

Thanks for your first contribution — we're thrilled to have you here.{custom}{checklist}

**Quick tips:**
- 📖 Read [CONTRIBUTING.md](CONTRIBUTING.md) before you start
- 💬 Use `/assign` on any open issue to pick it up
- ❓ Use `/help` to see all available bot commands
- 🙋 Ask questions freely — no question is too basic!

We look forward to working with you! 🚀"""
