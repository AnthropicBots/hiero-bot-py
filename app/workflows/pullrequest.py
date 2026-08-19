# app/workflows/pullrequest.py

from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.ai.reviewer import AIReviewer
from app.db.models import ReviewerRecommendation
from app.github.client import GitHubClient
from app.utils import audit
from app.utils.logger import get_logger

log = get_logger("workflow.pullrequest")

LABEL_PASS = "quality: ✅ passed"
LABEL_FAIL = "quality: ❌ needs work"

# Reviewer recommendation budget. A PR spreading across more than a handful of
# directories has no sharp ownership signal, so querying more of them buys
# noise rather than accuracy — and every extra directory is another API call.
MAX_PATHS_QUERIED = 5
COMMITS_PER_PATH = 30
MAX_RECOMMENDATIONS = 2


class QualityCheck:
    def __init__(self, name: str, passed: bool, detail: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


class PullRequestWorkflow:
    def __init__(self, gh: GitHubClient) -> None:
        self._gh = gh
        self._ai = AIReviewer()

    async def handle_pr_opened(self, ctx: dict, payload: dict , action:str) -> None:
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

        # Post or update quality report
        if checks:
            report = self._build_report(checks)
            comments = await self._gh.list_issue_comments(
                owner, repo, pr_number, inst
            )

            existing_comment = next(
                (
                    comment
                    for comment in comments
                    if comment.get("body", "").startswith("## 🔍 Quality Gate Report")
                ),
                None,
            )

            if existing_comment:
                await self._gh.update_comment(
                    owner,
                    repo,
                    existing_comment["id"],
                    report,
                    inst,
                )
            else:
                await self._gh.post_comment(
                    owner,
                    repo,
                    pr_number,
                    report,
                    inst,
                )

        # Label
        if cfg.auto_label:
            await self._gh.add_label(
                owner, repo, pr_number, LABEL_PASS if all_passed else LABEL_FAIL, inst
            )

        await audit.record(
            db,
            action="pr.labeled",
            owner=owner,
            repo=repo,
            target_number=pr_number,
            target_login=author,
            reason="Quality gates evaluated",
            metadata={
                "passed": all_passed,
                "failed_checks": [c.name for c in checks if not c.passed],
            },
        )

        # AI review
        if action in ("opened", "reopened") and cfg.ai_review.enabled:
            await self._run_ai_review(ctx, pr)

# Reviewer recommendation
        if action in ("opened", "reopened") and cfg.reviewer_recommendation:
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
            linked = bool(
                re.search(r"(?:closes|fixes|resolves)\s+#\d+", body, re.IGNORECASE)
            )
            checks.append(
                QualityCheck(
                    "Linked Issue",
                    linked,
                    (
                        "PR description references a closing issue ✅"
                        if linked
                        else "Add `Closes #N` to your PR description ❌"
                    ),
                )
            )

        # Tests
        if gates.require_tests:
            files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
            test_pats = [
                re.compile(p)
                for p in [
                    r"\.test\.[jt]sx?$",
                    r"\.spec\.[jt]sx?$",
                    r"tests?/",
                    r"test_.*\.py$",
                    r".*_test\.py$",
                ]
            ]
            has_tests = any(
                any(p.search(f["filename"]) for p in test_pats) for f in files
            )
            checks.append(
                QualityCheck(
                    "Tests",
                    has_tests,
                    (
                        "Changes include test coverage ✅"
                        if has_tests
                        else "Please add or update tests for your changes ❌"
                    ),
                )
            )

        # DCO
        if gates.require_dco:
            sha = pr.get("head", {}).get("sha", "")
            passed = await self._check_status(owner, repo, sha, "DCO", inst)
            checks.append(
                QualityCheck(
                    "DCO Sign-off",
                    passed,
                    (
                        "All commits are signed-off ✅"
                        if passed
                        else "Sign your commits with `git commit -s` — see [DCO](https://developercertificate.org/) ❌"
                    ),
                )
            )

        # GPG
        if gates.require_gpg_signature:
            commits = await self._gh.list_pr_commits(owner, repo, pr_number, inst)
            signed = all(
                (c.get("commit") or {}).get("verification", {}).get("verified")
                for c in commits
            )
            checks.append(
                QualityCheck(
                    "GPG Signature",
                    signed,
                    (
                        "All commits are GPG signed ✅"
                        if signed
                        else "Commits must be GPG signed — see [GitHub Docs](https://docs.github.com/en/authentication/managing-commit-signature-verification) ❌"
                    ),
                )
            )

        # Max files
        if gates.max_files_changed:
            n = pr.get("changed_files", 0)
            ok = n <= gates.max_files_changed
            checks.append(
                QualityCheck(
                    "PR Size",
                    ok,
                    (
                        f"PR size is fine ({n} files) ✅"
                        if ok
                        else f"PR too large ({n} files > {gates.max_files_changed}). Please split ❌"
                    ),
                )
            )

        # Branch pattern
        if gates.allowed_branch_pattern:
            branch = pr.get("head", {}).get("ref", "")
            ok = bool(re.match(gates.allowed_branch_pattern, branch))
            checks.append(
                QualityCheck(
                    "Branch Name",
                    ok,
                    (
                        f"Branch `{branch}` matches required pattern ✅"
                        if ok
                        else f"Branch `{branch}` must match `{gates.allowed_branch_pattern}` ❌"
                    ),
                )
            )

        # Changelog
        if gates.require_changelog_entry:
            files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
            has_cl = any(
                re.match(r"CHANGELOG|CHANGES|HISTORY", f["filename"], re.IGNORECASE)
                for f in files
            )
            checks.append(
                QualityCheck(
                    "Changelog",
                    has_cl,
                    (
                        "CHANGELOG entry included ✅"
                        if has_cl
                        else "Please add a CHANGELOG entry ❌"
                    ),
                )
            )

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
        status = (
            "✅ All quality gates passed!"
            if all_passed
            else "❌ Some gates need attention."
        )
        return f"""## 🔍 Quality Gate Report

{status}

| Status | Check | Details |
|--------|-------|---------|
{rows}

{"" if all_passed else "> Please address failing checks before requesting a review."}"""

    # ── AI Review & Recommendation ───────────────────────────────────────────── 

    async def _run_ai_review(self, ctx: dict, pr: dict) -> None:
        import base64

        cfg = ctx["config"].workflows.pull_request.ai_review
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        pr_number = pr["number"]
        sha = pr.get("head", {}).get("sha", "")

        files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
        diffs = [
            {"path": f["filename"], "diff": f.get("patch", "")}
            for f in files
            if f.get("patch")
        ][:15]

        file_contents = []
        for f in files[:8]:
            if f.get("status") == "removed":
                continue
            try:
                raw = await self._gh.get_file_content(
                    owner, repo, f["filename"], inst, ref=sha
                )
                if raw:
                    decoded = base64.b64decode(raw).decode("utf-8", errors="replace")
                    if len(decoded) <= 15000:
                        file_contents.append(
                            {"path": f["filename"], "content": decoded}
                        )
            except Exception:
                log.exception(
                    "Failed to fetch file content for %s/%s: %s",
                    owner,
                    repo,
                    f["filename"],
                )
                continue

        result = await self._ai.review(
            cfg, pr.get("title", ""), pr.get("body") or "", diffs, file_contents
        )

        emoji = (
            "🟢" if result["score"] >= 80 else "🟡" if result["score"] >= 60 else "🔴"
        )
        body = (
            f"## 🤖 AI Code Review\n\n"
            f"{emoji} **Score: {result['score']}/100** | `{result['verdict']}`\n\n"
            f"{result['summary']}\n\n"
            f"---\n_Automated AI review — a human maintainer will also review._"
        )
        await self._gh.post_comment(
            owner,
            repo,
            pr_number,
            body,
            inst,
        )

        for comment in result.get("comments", [])[: cfg.max_comments]:
            await self._gh.create_pr_review_comment(
                owner,
                repo,
                pr_number,
                f"{_sev_emoji(comment['severity'])} {comment['body']}",
                comment["path"],
                comment["line"],
                sha,
                inst,
            )

        await audit.record(
            ctx["db"],
            action="pr.reviewed",
            owner=owner,
            repo=repo,
            target_number=pr_number,
            target_login=pr["user"]["login"],
            reason=f"AI review score={result['score']}",
            metadata={"score": result["score"], "verdict": result["verdict"]},
        )

    # ── Reviewer recommendation ───────────────────────────────

    async def _recommend_reviewers(self, ctx: dict, pr: dict) -> None:
        """
        Suggest reviewers from the commit history of the directories this PR touches.

        The previous implementation listed the 50 most recent closed PRs and
        then fetched the file list of every one of them — up to 51 API calls per
        opened PR, serially, and it only ever saw whoever *authored* those PRs
        rather than who reviewed them. This asks GitHub for the commit history of
        each touched directory instead: a handful of concurrent requests, and the
        answer is real file history rather than a proxy for it.
        """
        owner, repo, inst = ctx["owner"], ctx["repo"], ctx["installation_id"]
        pr_number = pr["number"]
        author = pr["user"]["login"]

        try:
            files = await self._gh.list_pr_files(owner, repo, pr_number, inst)
            directories = _touched_directories(files)
            if not directories:
                return

            histories = await asyncio.gather(
                *(
                    self._gh.list_commits(
                        owner, repo, inst, path=directory, per_page=COMMITS_PER_PATH
                    )
                    for directory in directories
                ),
                return_exceptions=True,
            )

            scores = _score_candidates(histories, exclude=author)
            if not scores:
                return

            top = sorted(scores.items(), key=lambda item: item[1], reverse=True)[
                :MAX_RECOMMENDATIONS
            ]
            best = top[0][1]

            for login, hits in top:
                reason = (
                    f"{hits} recent commit(s) in "
                    f"{', '.join(sorted(directories))}"
                )
                score = round(hits / best, 3)

                existing = await ctx["db"].scalar(
                    select(ReviewerRecommendation).where(
                        ReviewerRecommendation.owner == owner,
                        ReviewerRecommendation.repo == repo,
                        ReviewerRecommendation.pr_number == pr_number,
                        ReviewerRecommendation.recommended_reviewer == login,
                    )
                )

                if existing is None:
                    ctx["db"].add(
                        ReviewerRecommendation(
                            owner=owner,
                            repo=repo,
                            pr_number=pr_number,
                            recommended_reviewer=login,
                            reason=reason,
                            score=score,
                            was_assigned=False,
                        )
                    )
                else:
                    existing.reason = reason
                    existing.score = score

            names = ", ".join(f"@{login}" for login, _ in top)
            await self._gh.post_comment(
                owner,
                repo,
                pr_number,
                f"💡 **Suggested reviewers** based on relevant file history: {names}",
                inst,
            )

            await audit.record(
                ctx["db"],
                action="pr.reviewer_recommended",
                owner=owner,
                repo=repo,
                target_number=pr_number,
                reason="Reviewer recommendation based on directory commit history",
                metadata={
                    "recommendations": [login for login, _ in top],
                    "directories": sorted(directories),
                    "api_calls": len(directories) + 1,
                },
            )
        except Exception as exc:
            log.warning("Reviewer recommendation failed: %s", exc)


def _touched_directories(files: list[dict]) -> list[str]:
    """
    The directories this PR changes, most-touched first, capped.

    Files at the repository root collapse to "", which GitHub rejects as a path
    filter, so they are dropped — a root-only PR has no meaningful ownership
    signal to extract anyway.
    """
    counts: dict[str, int] = {}
    for f in files:
        filename = f.get("filename") or ""
        if "/" not in filename:
            continue
        directory = filename.rsplit("/", 1)[0]
        counts[directory] = counts.get(directory, 0) + 1

    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
    return [directory for directory, _ in ranked[:MAX_PATHS_QUERIED]]


def _score_candidates(histories: list, exclude: str) -> dict[str, int]:
    """Count unique commits per author across the fetched histories, skipping bots."""
    scores: dict[str, int] = {}
    seen_shas: set[str] = set()

    for history in histories:
        if isinstance(history, BaseException):
            log.debug("Commit history lookup failed: %s", history)
            continue

        for commit in history or []:
            sha = commit.get("sha")
            if sha:
                if sha in seen_shas:
                    continue
                seen_shas.add(sha)

            login = ((commit.get("author") or {}).get("login") or "").strip()
            if not login or login == exclude:
                continue
            if login.lower().endswith("[bot]"):
                continue
            scores[login] = scores.get(login, 0) + 1

    return scores


def _sev_emoji(sev: str) -> str:
    return {"error": "🔴", "warning": "🟡", "info": "ℹ️"}.get(sev, "ℹ️")
