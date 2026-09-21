"""A failing PR step must not silently skip the steps after it."""

from unittest.mock import MagicMock, patch

import httpx

from app.github.webhooks import WebhookRouter
from app.workflows.prhealth import PRHealthWorkflow
from app.workflows.pullrequest import PullRequestWorkflow
from app.workflows.reviewerassignment import ReviewerAssignmentWorkflow

PAYLOAD = {
    "action": "opened",
    "pull_request": {"number": 1, "draft": False, "user": {"login": "author"}},
}
CTX = {"owner": "o", "repo": "r", "installation_id": 1, "config": None, "db": None}


def _http_422() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.github.com/x")
    return httpx.HTTPStatusError("422", request=req, response=httpx.Response(422, request=req))


async def _run(failing: str) -> list[str]:
    calls: list[str] = []

    def step(name: str):
        async def _step(*_a, **_k):
            calls.append(name)
            if name == failing:
                raise _http_422()

        return _step

    router = WebhookRouter(MagicMock(), MagicMock())
    with (
        patch.object(PullRequestWorkflow, "handle_pr_opened", step("quality")),
        patch.object(ReviewerAssignmentWorkflow, "handle_pr_opened", step("reviewer")),
        patch.object(PRHealthWorkflow, "score_pr", step("health")),
    ):
        await router._dispatch("pull_request", PAYLOAD, CTX)
    return calls


async def test_reviewer_failure_does_not_skip_health_scoring():
    assert await _run("reviewer") == ["quality", "reviewer", "health"]


async def test_quality_failure_does_not_skip_later_steps():
    assert await _run("quality") == ["quality", "reviewer", "health"]


async def test_no_failure_runs_all_steps_in_order():
    assert await _run("none") == ["quality", "reviewer", "health"]
