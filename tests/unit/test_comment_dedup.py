"""The bot keeps ONE report and ONE health comment per PR, and only edits its own."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.utils.comments import find_bot_comment, is_bot_comment
from app.workflows.prhealth import HEALTH_MARKER, PRHealthWorkflow
from app.workflows.pullrequest import PullRequestWorkflow

REPORT = "## 🔍 Quality Gate Report"


def _pr(body: str) -> dict:
    return {
        "number": 7,
        "title": "docs: x",
        "body": body,
        "user": {"login": "alice"},
        "head": {"sha": "abc", "ref": "docs/x"},
        "additions": 5,
        "deletions": 0,
        "changed_files": 1,
        "draft": False,
    }


class Thread:
    """A tiny stateful comment thread so repeated pushes behave like GitHub."""

    def __init__(self, gh) -> None:
        self.comments: list[dict] = []
        self._next = 100
        gh.list_issue_comments = AsyncMock(side_effect=lambda *a, **k: list(self.comments))
        gh.post_comment = AsyncMock(side_effect=self._post)
        gh.update_comment = AsyncMock(side_effect=self._update)

    async def _post(self, owner, repo, number, body, inst):
        self.comments.append({"id": self._next, "body": body, "user": {"type": "Bot"}})
        self._next += 1

    async def _update(self, owner, repo, comment_id, body, inst):
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body


def _ptet_health(ctx):
    """The ptet-web scoring setup: description + linked issue + small diff = 65 (healthy)."""
    cfg = ctx["config"].workflows.pr_health
    cfg.score_weights = {
        "has_tests": 0.10,
        "has_linked_issue": 0.25,
        "has_description": 0.25,
        "review_count": 0.25,
        "small_diff": 0.15,
    }
    cfg.label_healthy_above = 60
    cfg.comment_threshold = 40
    return ctx


def _health_comments(thread: Thread) -> list[dict]:
    return [c for c in thread.comments if HEALTH_MARKER in c["body"]]


# ── helpers ──────────────────────────────────────────────────


def test_is_bot_comment_requires_bot_author():
    assert is_bot_comment({"user": {"type": "Bot"}})
    assert not is_bot_comment({"user": {"type": "User"}})
    assert not is_bot_comment({})


def test_find_bot_comment_ignores_human_lookalike():
    comments = [{"id": 1, "user": {"type": "User"}, "body": REPORT + "\nfake"}]
    assert find_bot_comment(comments, prefix=REPORT) is None


# ── health comment ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_comment_is_edited_not_stacked(mock_gh, ctx):
    thread = Thread(mock_gh)
    wf = PRHealthWorkflow(mock_gh)

    for _ in range(3):  # three pushes to a PR that stays 'needs work'
        await wf.score_pr(ctx, {"pull_request": _pr("fix stuff")})

    assert len(_health_comments(thread)) == 1
    assert mock_gh.post_comment.await_count == 1
    assert mock_gh.update_comment.await_count == 2


@pytest.mark.asyncio
async def test_health_comment_updated_when_pr_becomes_healthy(mock_gh, ctx):
    _ptet_health(ctx)
    thread = Thread(mock_gh)
    wf = PRHealthWorkflow(mock_gh)

    await wf.score_pr(ctx, {"pull_request": _pr("fix stuff")})
    assert "below" in _health_comments(thread)[0]["body"]

    good = _pr("Adds a clear description of the change that is well over fifty characters.\n\nCloses #1")
    await wf.score_pr(ctx, {"pull_request": good})

    comments = _health_comments(thread)
    assert len(comments) == 1  # edited, not added
    assert "now above" in comments[0]["body"]


@pytest.mark.asyncio
async def test_healthy_pr_with_no_prior_comment_stays_quiet(mock_gh, ctx):
    _ptet_health(ctx)
    thread = Thread(mock_gh)
    good = _pr("Adds a clear description of the change that is well over fifty characters.\n\nCloses #1")

    await PRHealthWorkflow(mock_gh).score_pr(ctx, {"pull_request": good})

    assert thread.comments == []


@pytest.mark.asyncio
async def test_health_comment_falls_back_to_post_if_edit_fails(mock_gh, ctx):
    thread = Thread(mock_gh)
    thread.comments.append({"id": 5, "body": HEALTH_MARKER + "\nold", "user": {"type": "Bot"}})
    req = httpx.Request("PATCH", "https://api.github.com/x")
    mock_gh.update_comment = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
    )

    await PRHealthWorkflow(mock_gh).score_pr(ctx, {"pull_request": _pr("fix stuff")})

    mock_gh.post_comment.assert_awaited_once()


# ── quality report ───────────────────────────────────────────


def _quality_ctx(ctx):
    cfg = ctx["config"].workflows.pull_request
    cfg.ai_review.enabled = False
    cfg.reviewer_recommendation = False
    cfg.quality_gates.require_linked_issue = True
    cfg.quality_gates.require_tests = False
    cfg.quality_gates.require_dco = False
    return ctx


@pytest.mark.asyncio
async def test_quality_report_never_edits_a_humans_comment(mock_gh, ctx):
    _quality_ctx(ctx)
    thread = Thread(mock_gh)
    thread.comments.append({"id": 1, "body": REPORT + "\nfake", "user": {"type": "User", "login": "mallory"}})

    await PullRequestWorkflow(mock_gh).handle_pr_opened(ctx, {"pull_request": _pr("Closes #1")}, "synchronize")

    mock_gh.update_comment.assert_not_awaited()
    assert thread.comments[0]["body"] == REPORT + "\nfake"  # untouched
    assert len([c for c in thread.comments if c["user"]["type"] == "Bot"]) == 1  # bot posted its own


@pytest.mark.asyncio
async def test_quality_report_survives_push_then_reopen(mock_gh, ctx):
    _quality_ctx(ctx)
    thread = Thread(mock_gh)
    wf = PullRequestWorkflow(mock_gh)
    payload = {"pull_request": _pr("Closes #1")}

    await wf.handle_pr_opened(ctx, payload, "synchronize")
    await wf.handle_pr_opened(ctx, payload, "reopened")

    reports = [c for c in thread.comments if c["body"].startswith(REPORT)]
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_quality_report_falls_back_to_post_if_edit_fails(mock_gh, ctx):
    _quality_ctx(ctx)
    thread = Thread(mock_gh)
    thread.comments.append({"id": 9, "body": REPORT + "\nold", "user": {"type": "Bot"}})
    req = httpx.Request("PATCH", "https://api.github.com/x")
    mock_gh.update_comment = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
    )

    with patch("app.workflows.pullrequest.AIReviewer"):
        await PullRequestWorkflow(mock_gh).handle_pr_opened(ctx, {"pull_request": _pr("Closes #1")}, "synchronize")

    mock_gh.post_comment.assert_awaited_once()
