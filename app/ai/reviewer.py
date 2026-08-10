# app/ai/reviewer.py — Structured code review over a pluggable model backend

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.ai.backends import (
    BackendError,
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
- Never hallucinate file paths, line numbers, or function names — only reference what's literally in the file content or diff given to you
- If the code is genuinely clean, say so plainly in the summary instead of inventing minor nitpicks to fill space
- Respond with valid JSON ONLY — no markdown fences, no preamble, no reasoning shown"""


MAX_TOKENS = 4096

# Backoff between retries, in seconds. Overridable so tests don't sleep.
RETRY_BASE_DELAY = 1.0


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
            self._backend = build_backend(getattr(cfg, "provider", "auto"))
            log.info("AI review using the %s backend", self._backend.name)
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

        prompt = self._build_prompt(pr_title, pr_body, diffs, file_contents or [], cfg)
        request = CompletionRequest(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            model=cfg.model,
            max_tokens=MAX_TOKENS,
            timeout_seconds=getattr(cfg, "timeout_seconds", 60),
        )

        try:
            text = await self._complete_with_retries(cfg, request)
        except BackendUnavailable as exc:
            # Misconfiguration, not a transient failure — retrying a missing
            # API key just wastes the PR author's time waiting.
            log.error("AI review backend unavailable: %s", exc)
            return _unavailable()
        except BackendError as exc:
            log.error("AI review failed: %s", exc)
            return _unavailable()
        except Exception:
            log.exception("Unexpected AI review failure")
            return _unavailable()

        return self._parse(text)

    async def _complete_with_retries(self, cfg, request: CompletionRequest) -> str:
        backend = self._get_backend(cfg)
        attempts = getattr(cfg, "max_retries", 2) + 1
        last_error: BackendError | None = None

        for attempt in range(attempts):
            try:
                return await backend.complete(request)
            except BackendUnavailable:
                raise  # Retrying a missing API key never helps.
            except BackendError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    delay = RETRY_BASE_DELAY * (2**attempt)
                    log.warning(
                        "AI review attempt %d/%d failed (%s) — retrying in %.1fs",
                        attempt + 1,
                        attempts,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise last_error or BackendError("AI review produced no response")

    @staticmethod
    def _build_prompt(
        pr_title: str, pr_body: str, diffs: list, file_contents: list, cfg
    ) -> str:
        focus = ", ".join(cfg.focus_areas)
        diff_text = "\n\n".join(
            f"**{d['path']}**\n```diff\n{d['diff'][:6000]}\n```" for d in diffs[:15]
        )
        files_text = "\n\n".join(
            f"**Full content — {f['path']}**\n```\n{f['content']}\n```"
            for f in file_contents
        )
        files_block = (
            f"\n\n**Full file contents (for context):**\n{files_text}\n"
            if files_text
            else ""
        )

        return f"""Review this pull request. Judge the code only — ignore PR title/description quality.

**Title:** {pr_title}
**Description:** {pr_body or '(none)'}
**Focus areas:** {focus}
**Max inline comments:** {cfg.max_comments}
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
        try:
            clean = (
                text.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )

            match = re.search(r"\{.*\}", clean, re.DOTALL)
            clean = match.group(0) if match else clean
            parsed = json.loads(clean)
            return {
                "summary": str(parsed.get("summary", "")),
                "verdict": parsed.get("verdict", "comment")
                           if parsed.get("verdict") in ("approve", "request_changes", "comment")
                           else "comment",
                "score": max(0, min(100, int(parsed.get("score", 50)))),
                "comments": [
                    {
                        "path": str(c.get("path", "")),
                        "line": max(1, int(c.get("line", 1))),
                        "body": str(c.get("body", "")),
                        "severity": c.get("severity", "info")
                                    if c.get("severity") in ("info", "warning", "error")
                                    else "info",
                    }
                    for c in (parsed.get("comments") or [])[:20]
                    if c.get("path") and c.get("body")
                ],
            }
        except Exception as exc:
            log.warning("Failed to parse AI response: %s | raw=%s", exc, text[:300])
            return {
                "summary": "_AI review could not be parsed this time — the model's response wasn't valid JSON. This usually clears up on retry._",
                "verdict": "comment",
                "score": 50,
                "comments": [],
            }

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()