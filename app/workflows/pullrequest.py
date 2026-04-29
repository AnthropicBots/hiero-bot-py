# app/workflows/pullrequest.py

from __future__ import annotations
import re
from app.github.client import GitHubClient
from app.ai.reviewer import AIReviewer
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.pullrequest")

LABEL_PASS = "quality: ✅ passed"
LABEL_FAIL = "quality: ❌ needs work"


class QualityCheck:
    def __init__(self, name: str, passed: bool, detail: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


class PullRequestWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh
        self._ai = AIReviewer()

    async def handle_pr_opened(self, ctx: dict, payload: dict) -> None:
        cfg = ctx["config"].workflows.pull_request
        if not cfg.enabled:
            return

        pr = payload.get("pull_request", {})
        if not pr:
            return

        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        db = ctx["db"]
        pr_number = pr["number"]
        author = pr["user"]["login"]

        checks = await self._run_quality_checks(ctx, pr)
        all_passed = all(c.passed for c in checks)

        # Post quality report
        if checks:
            await self._gh.post_comment(owner, repo, pr_number,
                                        self._build_report(checks), inst)

        # Label
        if cfg.auto_label:
            await self._gh.add_label(
                owner, repo, pr_number,
                LABEL_PASS if all_passed else LABEL_FAIL, inst
            )

        await audit.record(
            db, action="pr.labeled", owner=owner, repo=repo,
            target_number=pr_number, target_login=author,
            reason="Quality gates evaluated",
            metadata={
                "passed": all_passed,
                "failed_checks": [c.name for c in checks if not c.passed],
            },
        )

        # AI review
        if cfg.ai_review.enabled:
            await self._run_ai_review(ctx, pr)

        # Reviewer recommendation
        if cfg.reviewer_recommendation:
            await self._recommend_reviewers(ctx, pr)

        await db.commit()

    # ── Quality gates ─────────────────────────────────────────

    async def _run_quality_checks(self, ctx: dict, pr: dict) -> list[QualityCheck]:
        gates = ctx["config"].workflows.pull_request.quality_gates
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        pr_number = pr["number"]
        checks: list[QualityCheck] = []

        # Linked issue
        if gates.require_linked_issue:
            body = pr.get("body") or ""
            linked = bool(re.search(r"(?:closes|fixes|resolves)\s+#\d+", body, re.I))
            checks.append(QualityCheck(
                "Linked Issue", linked,
                "PR description references a closing issue ✅" if linked
                else 'Add `Closes #N` to your PR description ❌',
            ))

        # Tests
        if gates.require_tests:
            files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
            test_pats = [re.compile(p) for p in
                         [r"\.test\.[jt]sx?$", r"\.spec\.[jt]sx?$",
                          r"tests?/", r"test_.*\.py$", r".*_test\.py$"]]
            has_tests = any(
                any(p.search(f["filename"]) for p in test_pats) for f in files
            )
            checks.append(QualityCheck(
                "Tests", has_tests,
                "Changes include test coverage ✅" if has_tests
                else "Please add or update tests for your changes ❌",
            ))

        # DCO
        if gates.require_dco:
            sha = pr.get("head", {}).get("sha", "")
            passed = await self._check_status(owner, repo, sha, "DCO", inst)
            checks.append(QualityCheck(
                "DCO Sign-off", passed,
                "All commits are signed-off ✅" if passed
                else "Sign your commits with `git commit -s` — see [DCO](https://developercertificate.org/) ❌",
            ))

        # GPG
        if gates.require_gpg_signature:
            commits = await self._gh.list_pr_commits(owner, repo, pr_number, inst)
            signed = all(
                (c.get("commit") or {}).get("verification", {}).get("verified")
                for c in commits
            )
            checks.append(QualityCheck(
                "GPG Signature", signed,
                "All commits are GPG signed ✅" if signed
                else "Commits must be GPG signed — see [GitHub Docs](https://docs.github.com/en/authentication/managing-commit-signature-verification) ❌",
            ))

        # Max files
        if gates.max_files_changed:
            n = pr.get("changed_files", 0)
            ok = n <= gates.max_files_changed
            checks.append(QualityCheck(
                "PR Size", ok,
                f"PR size is fine ({n} files) ✅" if ok
                else f"PR too large ({n} files > {gates.max_files_changed}). Please split ❌",
            ))

        # Branch pattern
        if gates.allowed_branch_pattern:
            branch = pr.get("head", {}).get("ref", "")
            ok = bool(re.match(gates.allowed_branch_pattern, branch))
            checks.append(QualityCheck(
                "Branch Name", ok,
                f"Branch `{branch}` matches required pattern ✅" if ok
                else f"Branch `{branch}` must match `{gates.allowed_branch_pattern}` ❌",
            ))

        # Changelog
        if gates.require_changelog_entry:
            if "files" not in dir(self):  # avoid re-fetching
                files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
            has_cl = any(
                re.match(r"CHANGELOG|CHANGES|HISTORY", f["filename"], re.I)
                for f in files
            )
            checks.append(QualityCheck(
                "Changelog", has_cl,
                "CHANGELOG entry included ✅" if has_cl
                else "Please add a CHANGELOG entry ❌",
            ))

        return checks

    async def _check_status(
        self, owner: str, repo: str, sha: str, context: str, inst: int
    ) -> bool:
        try:
            data = await self._gh.get_combined_status(owner, repo, sha, inst)
            statuses = data.get("statuses", [])
            match = next((s for s in statuses if context in s.get("context", "")), None)
            return match is not None and match["state"] == "success"
        except Exception:
            return True  # Fail open

    @staticmethod
    def _build_report(checks: list[QualityCheck]) -> str:
        all_passed = all(c.passed for c in checks)
        rows = "\n".join(
            f"| {'✅' if c.passed else '❌'} | **{c.name}** | {c.detail} |"
            for c in checks
        )
        status = "✅ All quality gates passed!" if all_passed else "❌ Some gates need attention."
        return f"""## 🔍 Quality Gate Report

{status}

| Status | Check | Details |
|--------|-------|---------|
{rows}

{"" if all_passed else "> Please address failing checks before requesting a review."}"""

    # ── AI Review ─────────────────────────────────────────────

    async def _run_ai_review(self, ctx: dict, pr: dict) -> None:
        cfg = ctx["config"].workflows.pull_request.ai_review
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        pr_number = pr["number"]

        files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
        diffs = [
            {"path": f["filename"], "diff": f.get("patch", "")}
            for f in files if f.get("patch")
        ][:15]

        result = await self._ai.review(
            cfg, pr.get("title", ""), pr.get("body") or "", diffs
        )

        emoji = "🟢" if result["score"] >= 80 else "🟡" if result["score"] >= 60 else "🔴"
        body = (
            f"## 🤖 AI Code Review\n\n"
            f"{emoji} **Score: {result['score']}/100** | `{result['verdict']}`\n\n"
            f"{result['summary']}\n\n"
            f"---\n_Automated AI review — a human maintainer will also review._"
        )
        await self._gh.post_comment(owner, repo, pr_number, body, inst)

        sha = pr.get("head", {}).get("sha", "")
        for comment in result.get("comments", [])[:cfg.max_comments]:
            await self._gh.create_pr_review_comment(
                owner, repo, pr_number,
                f"{_sev_emoji(comment['severity'])} {comment['body']}",
                comment["path"], comment["line"], sha, inst,
            )

        await audit.record(
            ctx["db"], action="pr.reviewed", owner=owner, repo=repo,
            target_number=pr_number, target_login=pr["user"]["login"],
            reason=f"AI review score={result['score']}",
            metadata={"score": result["score"], "verdict": result["verdict"]},
        )

    # ── Reviewer recommendation ───────────────────────────────

    async def _recommend_reviewers(self, ctx: dict, pr: dict) -> None:
        """Suggest reviewers based on recent contribution history."""
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        pr_number = pr["number"]
        author = pr["user"]["login"]

        try:
            files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
            touched_paths = [f["filename"] for f in files]

            # Get recent closed PRs touching same paths
            recent_prs = await self._gh.get(
                f"/repos/{owner}/{repo}/pulls", inst,
                params={"state": "closed", "per_page": 50}
            )

            scores: dict[str, float] = {}
            for rpr in (recent_prs or []):
                reviewer = (rpr.get("user") or {}).get("login", "")
                if not reviewer or reviewer == author:
                    continue
                pr_files = await self._gh.list_pr_files(owner, repo, rpr["number"], inst)
                overlap = sum(
                    1 for f in pr_files
                    if any(f["filename"].rsplit("/", 1)[0] in p for p in touched_paths)
                )
                if overlap:
                    scores[reviewer] = scores.get(reviewer, 0) + overlap

            if not scores:
                return

            top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:2]
            names = ", ".join(f"@{r}" for r, _ in top)
            await self._gh.post_comment(
                owner, repo, pr_number,
                f"💡 **Suggested reviewers** based on relevant file history: {names}",
                inst,
            )

            await audit.record(
                ctx["db"], action="pr.reviewer_recommended", owner=owner, repo=repo,
                target_number=pr_number,
                reason="Reviewer recommendation based on file overlap",
                metadata={"recommendations": [r for r, _ in top]},
            )
        except Exception as exc:
            log.warning("Reviewer recommendation failed: %s", exc)


def _sev_emoji(sev: str) -> str:
    return {"error": "🔴", "warning": "🟡", "info": "ℹ️"}.get(sev, "ℹ️")
