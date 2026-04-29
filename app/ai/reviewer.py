# app/ai/reviewer.py — Anthropic-powered code review

from __future__ import annotations
import json
from typing import Any
from app.utils.settings import settings
from app.utils.logger import get_logger

log = get_logger("ai.reviewer")

SYSTEM_PROMPT = """You are a precise, constructive code reviewer for the Hiero open source project.

Rules:
- Be respectful and educational, especially for first-time contributors
- Flag real problems, not trivial nitpicks unless style is in focus areas
- Security issues must always be "error" severity
- Provide specific, actionable suggestions
- Never hallucinate file paths or line numbers — only reference what's in the diff
- Respond with valid JSON ONLY — no markdown fences, no preamble"""


class AIReviewer:
    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured")
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        return self._client

    async def review(
        self,
        cfg,
        pr_title: str,
        pr_body: str,
        diffs: list[dict[str, str]],
    ) -> dict[str, Any]:
        if not cfg.enabled:
            raise ValueError("AI review is disabled in config")

        prompt = self._build_prompt(pr_title, pr_body, diffs, cfg)
        try:
            client = self._get_client()
            response = await client.messages.create(
                model=cfg.model,
                max_tokens=2048,
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
    def _build_prompt(pr_title: str, pr_body: str, diffs: list, cfg) -> str:
        focus = ", ".join(cfg.focus_areas)
        diff_text = "\n\n".join(
            f"**{d['path']}**\n```diff\n{d['diff'][:2000]}\n```"
            for d in diffs[:10]
        )
        return f"""Review this pull request.

**Title:** {pr_title}
**Description:** {pr_body or '(none)'}
**Focus areas:** {focus}
**Max inline comments:** {cfg.max_comments}

**Diffs:**
{diff_text}

Respond with JSON only:
{{
  "summary": "1-2 paragraph overall assessment",
  "verdict": "approve" | "request_changes" | "comment",
  "score": 0-100,
  "comments": [
    {{
      "path": "path/to/file.py",
      "line": 42,
      "body": "Specific actionable feedback",
      "severity": "info" | "warning" | "error"
    }}
  ]
}}"""

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        try:
            clean = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
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
            log.warning("Failed to parse AI response: %s", exc)
            return {"summary": text[:300], "verdict": "comment", "score": 50, "comments": []}
