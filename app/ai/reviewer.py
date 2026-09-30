# app/ai/reviewer.py — Structured code review over a pluggable model backend

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.ai.backends import (
    BackendError,
    BackendTransientError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
    build_backend,
)
from app.utils.logger import get_logger

log = get_logger("ai.reviewer")


def _unavailable() -> dict[str, Any]:
    return {
        "summary": "_AI review unavailable at this time._",
        "verdict": "comment",
        "score": 50,
        "comments": [],
        "failed": True,
    }


SYSTEM_PROMPT = """You are a senior staff engineer doing a rigorous code review for the Hiero open source project. You take this seriously — sloppy or generic reviews waste contributors' time.

You are given the FULL CONTENT of the changed files (not just the diff) plus the diff itself, so you can see the surrounding code, imports, and how the change fits into the file. Use that context — don't review the diff in isolation.

For EVERY changed file, systematically check:
1. Correctness — logic errors, off-by-one bugs, wrong operators, incorrect assumptions, unhandled edge cases (empty input, None, zero, negative numbers, concurrent access)
2. Security — injection, unsafe deserialization, hardcoded secrets, missing auth checks, unvalidated input, path traversal, SSRF, insecure defaults
3. Error handling — bare excepts, swallowed exceptions, missing error paths, resources not released on failure
4. Concurrency — race conditions, missing locks, non-atomic check-then-act patterns, shared mutable state
5. Performance — N+1 queries, unnecessary loops/allocations, blocking calls in async code, unbounded growth
6. Tests — missing coverage for the new logic, especially edge cases and failure paths
7. API/contract changes — breaking changes, backward compatibility, unclear function signatures

Rules:
- Judge ONLY the code. Never comment on PR title or description quality, missing issue links, or DCO — those are checked elsewhere and are not your job.
- Be specific: cite the exact line and exact problem, never vague ("could be improved")
- Every comment must include WHY it matters and a concrete fix, not just "consider changing this"
- Security and correctness bugs are always "error" severity, regardless of focus_areas
- Do not flag something unless you can point to the exact mechanism by which it fails — no speculative "this might cause issues"
- Do not pad the review with restated diff content or praise-only comments; every comment must be actionable
- Be respectful and educational, especially for first-time contributors — explain the "why", don't just command
- Never hallucinate file paths, line numbers, or function names — only reference what's literally in the file content or diff given
- If the code is genuinely clean, say so plainly in the summary instead of inventing minor nitpicks to fill space
- Respond with valid JSON ONLY — no markdown fences, no preamble, no reasoning shown"""


MAX_TOKENS = 4096

# Backoff between retries, in seconds. Overridable so tests don't sleep.
RETRY_BASE_DELAY = 1.0

MAX_REVIEW_FILES = 15
MAX_DIFF_CHARS_PER_FILE = 6000
MAX_REVIEW_COMMENTS = 20
MAX_RETRY_LIMIT = 5


class AIReviewer:
    """
    Turns a pull request into a structured review.

    Owns prompt construction, retry policy and response parsing. Which model
    answers is the backend's business — see `app/ai/backends/`.
    """

    def __init__(self, backend: ReviewBackend | None = None) -> None:
        self._backend = backend

    def _get_backend(self, cfg) -> ReviewBackend:
        if self._backend is None:
            self._backend = build_backend(
                getattr(cfg, "provider", "auto")
            )
            log.info(
                "AI review using the %s backend",
                self._backend.name,
            )

        return self._backend

    async def review(
        self,
        cfg,
        pr_title: str,
        pr_body: str,
        diffs: list[dict[str, str]],
        file_contents: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        if not cfg.enabled:
            raise ValueError("AI review is disabled in config")

        prompt = self._build_prompt(
            pr_title,
            pr_body,
            diffs,
            file_contents or [],
            cfg,
        )

        request = CompletionRequest(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            model=cfg.model,
            max_tokens=MAX_TOKENS,
            timeout_seconds=getattr(cfg, "timeout_seconds", 60),
            # Do not force a temperature value here. The pre-backend
            # reviewer did not set one, so provider defaults remain unchanged.
            temperature=None,
        )

        try:
            text = await self._complete_with_retries(
                cfg,
                request,
            )

        except BackendUnavailable as exc:
            # Missing API keys or unavailable dependencies are not transient.
            log.error(
                "AI review backend unavailable: %s",
                exc,
            )
            return _unavailable()

        except BackendError as exc:
            log.error(
                "AI review failed: %s",
                exc,
            )
            return _unavailable()

        except Exception:
            log.exception(
                "Unexpected AI review failure"
            )
            return _unavailable()

        return self._parse(text)

    async def _complete_with_retries(
        self,
        cfg,
        request: CompletionRequest,
    ) -> str:
        backend = self._get_backend(cfg)

        configured_retries = getattr(
            cfg,
            "max_retries",
            2,
        )

        try:
            max_retries = int(configured_retries)
        except (TypeError, ValueError):
            log.warning(
                "Invalid AI max_retries=%r; using default of 2",
                configured_retries,
            )
            max_retries = 2

        max_retries = max(
            0,
            min(max_retries, MAX_RETRY_LIMIT),
        )

        attempts = max_retries + 1
        last_error: BackendTransientError | None = None

        for attempt in range(attempts):
            try:
                return await backend.complete(request)

            except BackendUnavailable:
                # Retrying a missing API key or unavailable dependency never
                # helps, so this remains outside the retry contract.
                raise

            except BackendTransientError as exc:
                last_error = exc

                if attempt + 1 >= attempts:
                    break

                delay = RETRY_BASE_DELAY * (2**attempt)

                log.warning(
                    "AI review attempt %d/%d failed transiently (%s) "
                    "— retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )

                await asyncio.sleep(delay)

            except BackendError:
                # Permanent backend failures are intentionally not retried.
                raise

        raise last_error or BackendError(
            "AI review produced no response"
        )

    @staticmethod
    def _build_prompt(
        pr_title: str,
        pr_body: str,
        diffs: list[dict[str, str]],
        file_contents: list[dict[str, str]],
        cfg,
    ) -> str:
        focus_areas = getattr(
            cfg,
            "focus_areas",
            [],
        )

        focus = ", ".join(
            str(area)
            for area in focus_areas
        )

        max_comments = getattr(
            cfg,
            "max_comments",
            5,
        )

        diff_blocks: list[str] = []

        for diff in diffs[:MAX_REVIEW_FILES]:
            if not isinstance(diff, dict):
                continue

            path = diff.get("path", "")
            content = diff.get("diff", "")

            if not isinstance(path, str):
                path = str(path)

            if not isinstance(content, str):
                content = str(content)

            diff_blocks.append(
                f"**{path}**\n"
                f"```diff\n"
                f"{content[:MAX_DIFF_CHARS_PER_FILE]}\n"
                f"```"
            )

        diff_text = "\n\n".join(diff_blocks)

        file_blocks: list[str] = []

        for file_content in file_contents:
            if not isinstance(file_content, dict):
                continue

            path = file_content.get("path", "")
            content = file_content.get("content", "")

            if not isinstance(path, str):
                path = str(path)

            if not isinstance(content, str):
                content = str(content)

            file_blocks.append(
                f"**Full content — {path}**\n"
                f"```\n"
                f"{content}\n"
                f"```"
            )

        files_text = "\n\n".join(file_blocks)

        files_block = (
            "\n\n**Full file contents (for context):**\n"
            f"{files_text}\n"
            if files_text
            else ""
        )

        return f"""Review this pull request. Judge the code only — ignore PR title/description quality.

**Title:** {pr_title}
**Description:** {pr_body or "(none)"}
**Focus areas:** {focus}
**Max inline comments:** {max_comments}
{files_block}
**Diffs:**
{diff_text}

Respond with JSON only:
{{
  "summary": "1-2 paragraph overall assessment of the CODE",
  "verdict": "approve" | "request_changes" | "comment",
  "score": 0-100,
  "comments": [
    {{
      "path": "path/to/file.py",
      "line": 42,
      "body": "Specific actionable feedback: what's wrong, why it matters, how to fix it",
      "severity": "info" | "warning" | "error"
    }}
  ]
}}"""

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            return {
                "summary": (
                    "_AI review could not be parsed this time — "
                    "the model returned an empty response._"
                ),
                "verdict": "comment",
                "score": 50,
                "comments": [],
                "failed": True,
            }

        try:
            clean = text.strip()

            if clean.startswith("```json"):
                clean = clean[len("```json"):].strip()
            elif clean.startswith("```"):
                clean = clean[len("```"):].strip()

            if clean.endswith("```"):
                clean = clean[:-3].strip()

            # The model is instructed to return JSON only, but extracting the
            # outer object makes parsing tolerant of accidental surrounding text.
            match = re.search(
                r"\{.*\}",
                clean,
                re.DOTALL,
            )

            if match:
                clean = match.group(0)

            parsed = json.loads(clean)

            if not isinstance(parsed, dict):
                raise ValueError(
                    "AI review response must be a JSON object"
                )

            summary = parsed.get("summary")
            verdict = parsed.get("verdict")
            score = parsed.get("score")
            comments = parsed.get("comments")

            if (
                not isinstance(summary, str)
                or not summary.strip()
            ):
                raise ValueError(
                    "AI review response has no valid summary"
                )

            if verdict not in (
                "approve",
                "request_changes",
                "comment",
            ):
                raise ValueError(
                    "AI review response has an invalid verdict"
                )

            if (
                isinstance(score, bool)
                or not isinstance(score, int)
            ):
                raise ValueError(
                    "AI review response has an invalid score"
                )

            if not isinstance(comments, list):
                raise ValueError(
                    "AI review response has invalid comments"
                )

            normalized_comments: list[dict[str, Any]] = []

            for comment in comments[:MAX_REVIEW_COMMENTS]:
                if not isinstance(comment, dict):
                    continue

                path = comment.get("path")
                line = comment.get("line", 1)
                body = comment.get("body")
                severity = comment.get("severity", "info")

                if not isinstance(path, str) or not path.strip():
                    continue

                if not isinstance(body, str) or not body.strip():
                    continue

                if (
                    isinstance(line, bool)
                    or not isinstance(line, int)
                ):
                    line = 1

                if line < 1:
                    line = 1

                if severity not in (
                    "info",
                    "warning",
                    "error",
                ):
                    severity = "info"

                normalized_comments.append(
                    {
                        "path": path.strip(),
                        "line": line,
                        "body": body.strip(),
                        "severity": severity,
                    }
                )

            return {
                "summary": summary.strip(),
                "failed": False,
                "verdict": verdict,
                "score": max(
                    0,
                    min(100, score),
                ),
                "comments": normalized_comments,
            }

        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            log.warning(
                "Failed to parse AI response: %s | raw=%s",
                exc,
                text[:300],
            )

            return {
                "summary": (
                    "_AI review could not be parsed this time — "
                    "the model's response wasn't valid JSON. "
                    "This usually clears up on retry._"
                ),
                "verdict": "comment",
                "score": 50,
                "comments": [],
                "failed": True,
            }

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
