from __future__ import annotations

import json
import re
from typing import Any

from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("ai.reviewer")

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


class AIReviewer:
    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            if settings.openai_api_key:
                import openai

                self._client = openai.AsyncOpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                )
                self._client_type = "openai"
            elif settings.anthropic_api_key:
                import anthropic

                self._client = anthropic.AsyncAnthropic(
                    api_key=settings.anthropic_api_key
                )
                self._client_type = "anthropic"
            else:
                raise RuntimeError("No AI API key configured")
        return self._client

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
        try:
            client = self._get_client()
            if self._client_type == "openai":
                response = await client.chat.completions.create(
                    model=cfg.model,
                    max_tokens=4096,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                )
                text = response.choices[0].message.content
            else:
                response = await client.messages.create(
                    model=cfg.model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.content[0].text
            return self._parse(text)
        except Exception as exc:
            log.error("AI review failed: %s", exc)
            return {
                "summary": "_AI review unavailable at this time._",
                "verdict": "comment",
                "score": 50,
                "comments": [],
            }

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
            match = re.search(r"\{.*\}", text, re.DOTALL)
            clean = match.group(0) if match else text.strip()
            parsed = json.loads(clean)
            return {
                "summary": str(parsed.get("summary", "")),
                "verdict": (
                    parsed.get("verdict", "comment")
                    if parsed.get("verdict")
                    in ("approve", "request_changes", "comment")
                    else "comment"
                ),
                "score": max(0, min(100, int(parsed.get("score", 50)))),
                "comments": [
                    {
                        "path": str(c.get("path", "")),
                        "line": max(1, int(c.get("line", 1))),
                        "body": str(c.get("body", "")),
                        "severity": (
                            c.get("severity", "info")
                            if c.get("severity") in ("info", "warning", "error")
                            else "info"
                        ),
                    }
                    for c in (parsed.get("comments") or [])[:20]
                    if c.get("path") and c.get("body")
                ],
            }
        except Exception as exc:
            log.warning("Failed to parse AI response: %s", exc)
            return {
                "summary": "_AI review could not be parsed this time — the model's response wasn't valid JSON. This usually clears up on retry._",
                "verdict": "comment",
                "score": 50,
                "comments": [],
            }
