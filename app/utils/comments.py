# app/utils/comments.py — helpers for finding the bot's own comments

from __future__ import annotations

from typing import Any


def is_bot_comment(comment: dict[str, Any]) -> bool:
    """True when GitHub reports the comment's author as a Bot account.

    The bot only ever edits comments it wrote. Matching on body text alone would
    let any human whose comment starts with the same heading hijack the bot's
    report: GitHub rejects the edit (403) and the report is never posted again.
    """
    return ((comment.get("user") or {}).get("type") or "") == "Bot"


def find_bot_comment(
    comments: list[dict[str, Any]], *, prefix: str | None = None, marker: str | None = None
) -> dict[str, Any] | None:
    """First bot-authored comment whose body starts with `prefix` or contains `marker`."""
    for comment in comments:
        if not is_bot_comment(comment):
            continue
        body = comment.get("body") or ""
        if (prefix is not None and body.startswith(prefix)) or (
            marker is not None and marker in body
        ):
            return comment
    return None
