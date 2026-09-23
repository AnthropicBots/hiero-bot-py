# app/utils/safe_regex.py — bounded matching for owner-supplied regular expressions

from __future__ import annotations

import regex

from app.utils.logger import get_logger

log = get_logger("utils.safe_regex")

# A repository's config supplies the pattern and other people (PR authors) choose
# the text it is matched against, on the single event loop that serves every
# tenant. A catastrophic pattern must not be able to stall it.
MAX_PATTERN_LENGTH = 200
MAX_SUBJECT_LENGTH = 255
MATCH_TIMEOUT_SECONDS = 0.05


def validate_pattern(pattern: str) -> str:
    """Return ``pattern`` if it is usable, otherwise raise ``ValueError``."""
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"pattern is longer than {MAX_PATTERN_LENGTH} characters")
    try:
        regex.compile(pattern)
    except regex.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    return pattern


def bounded_match(pattern: str, text: str) -> bool:
    """``re.match`` semantics with a time limit; a timeout counts as no match."""
    try:
        return (
            regex.match(
                pattern, text[:MAX_SUBJECT_LENGTH], timeout=MATCH_TIMEOUT_SECONDS
            )
            is not None
        )
    except TimeoutError:
        log.warning(
            "Regular expression %r timed out after %d ms; treating it as no match",
            pattern,
            int(MATCH_TIMEOUT_SECONDS * 1000),
        )
        return False
    except regex.error as exc:  # a pattern that predates validation
        log.warning("Regular expression %r is invalid: %s", pattern, exc)
        return False
