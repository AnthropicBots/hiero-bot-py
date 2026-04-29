# app/workflows/onboarding.py

from __future__ import annotations
from datetime import datetime, timezone
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.onboarding")

BOT_PATTERNS = ("bot", "dependabot", "renovate", "github-actions", "[bot]")


class OnboardingWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh

    async def handle_new_contributor(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.onboarding
        if not cfg.enabled:
            return

        sender = payload.get("sender", {})
        login: str = sender.get("login", "")
        issue_number: int | None = (payload.get("issue") or {}).get("number")
        if not login or not issue_number:
            return

        # Skip bots
        if sender.get("type") == "Bot" or any(p in login.lower() for p in BOT_PATTERNS):
            log.debug("Skipping bot account: %s", login)
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db = ctx["db"]

        # Check first-time contributor
        if not await self._is_first_time(owner, repo, login, inst):
            return

        # Welcome comment
        msg = self._build_welcome(login, cfg)
        await self._gh.post_comment(owner, repo, issue_number, msg, inst)

        await audit.record(db, action="contributor.welcomed", owner=owner, repo=repo,
                           target_number=issue_number, target_login=login,
                           reason="First-time contributor")
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

        # Already assigned?
        assignees = [a["login"] for a in (issue.get("assignees") or [])]
        if login in assignees:
            await self._gh.post_comment(owner, repo, issue_number,
                                        f"@{login} You're already assigned! 🎉", inst)
            return

        # Eligibility check
        ok, reason = await self._check_eligibility(ctx, login)
        if not ok:
            await self._gh.post_comment(owner, repo, issue_number, reason, inst)
            return

        await self._gh.add_assignees(owner, repo, issue_number, [login], inst)
        await self._gh.post_comment(
            owner, repo, issue_number,
            f"✅ @{login} has been assigned! Good luck — ask questions any time.", inst
        )
        await audit.record(db, action="issue.assigned", owner=owner, repo=repo,
                           target_number=issue_number, target_login=login,
                           reason="Self-assignment via /assign")
        await db.commit()

    # ── Helpers ───────────────────────────────────────────────

    async def _is_first_time(self, owner: str, repo: str, login: str, inst: int) -> bool:
        try:
            contributors = await self._gh.get(
                f"/repos/{owner}/{repo}/contributors", inst,
                params={"per_page": 500}
            )
            return not any(c["login"] == login for c in (contributors or []))
        except Exception:
            return False

    async def _check_eligibility(self, ctx: dict, login: str) -> tuple[bool, str]:
        cfg = ctx["config"].workflows.onboarding
        try:
            user = await self._gh.get_user(login, ctx["installation_id"])
            created = datetime.fromisoformat(
                user["created_at"].replace("Z", "+00:00")
            )
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
        except Exception:
            pass  # Fail open
        return True, ""

    async def _assign_mentor(self, ctx: dict, issue_number: int, contributor: str) -> None:
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

        await self._gh.add_assignees(ctx["owner"], ctx["repo"], issue_number, [mentor], inst)
        await self._gh.post_comment(
            ctx["owner"], ctx["repo"], issue_number,
            f"👋 @{mentor} has been assigned as mentor to support @{contributor}.",
            inst
        )
        await audit.record(db, action="contributor.mentor_assigned",
                           owner=ctx["owner"], repo=ctx["repo"],
                           target_number=issue_number, target_login=contributor,
                           reason=f"Mentor @{mentor} assigned via {strategy}",
                           metadata={"mentor": mentor})
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
