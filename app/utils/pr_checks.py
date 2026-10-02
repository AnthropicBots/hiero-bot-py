# app/utils/pr_checks.py — PR check logic shared by PullRequestWorkflow (quality
# gates) and PRHealthWorkflow (health score) so the two never disagree (issue #123).

from __future__ import annotations

import re

# Anchored so `contest_utils.py` or `src/latest/handler.py` don't count as tests:
# each pattern must start at the beginning of the path or right after a "/".
TEST_FILE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(?:^|/)[^/]*\.test\.[jt]sx?$",
        r"(?:^|/)[^/]*\.spec\.[jt]sx?$",
        r"(?:^|/)(?:tests?|__tests__)/",
        r"(?:^|/)test_[^/]*\.py$",
        r"(?:^|/)[^/]*_test\.py$",
    )
)

SIGNED_OFF_RE = re.compile(
    r"^Signed-off-by:\s+\S.*<[^<>\s]+@[^<>\s]+>\s*$", re.IGNORECASE | re.MULTILINE
)


def is_test_file(filename: str) -> bool:
    """True if `filename` (a repo-relative path) looks like a test file."""
    return any(p.search(filename) for p in TEST_FILE_PATTERNS)


def commits_have_signoff(commits: list[dict]) -> bool:
    """True if there is at least one non-merge commit and every non-merge commit
    carries a `Signed-off-by:` trailer."""
    relevant = [c for c in commits if len(c.get("parents") or []) <= 1]
    return bool(relevant) and all(
        SIGNED_OFF_RE.search((c.get("commit") or {}).get("message") or "")
        for c in relevant
    )
