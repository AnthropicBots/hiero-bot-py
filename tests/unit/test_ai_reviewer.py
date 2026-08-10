# tests/unit/test_ai_reviewer.py

import json

import pytest

from app.ai import reviewer as reviewer_module
from app.ai.backends import BackendError, BackendUnavailable, ReviewBackend
from app.ai.reviewer import AIReviewer
from app.config.schema import AIReviewConfig

CFG = AIReviewConfig(enabled=True, max_comments=5, focus_areas=["security", "logic"])
DIFFS = [{"path": "src/auth.py", "diff": "+password = 'admin123'"}]


class StubBackend(ReviewBackend):
    """A backend that replays canned responses, so no provider SDK is involved."""

    name = "stub"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    @classmethod
    def available(cls):
        return True

    async def complete(self, request):
        self.requests.append(request)
        result = self.responses.pop(0) if self.responses else ""
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(reviewer_module, "RETRY_BASE_DELAY", 0)


def review_json(**overrides):
    payload = {
        "summary": "Hardcoded credential found.",
        "verdict": "request_changes",
        "score": 35,
        "comments": [
            {
                "path": "src/auth.py",
                "line": 1,
                "body": "Never hardcode passwords.",
                "severity": "error",
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


# ── Review flow ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_raises_when_disabled():
    disabled = AIReviewConfig(enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        await AIReviewer(StubBackend()).review(disabled, "title", "body", DIFFS)


@pytest.mark.asyncio
async def test_returns_parsed_review():
    result = await AIReviewer(StubBackend(review_json())).review(
        CFG, "feat: auth", "Adds login", DIFFS
    )

    assert result["verdict"] == "request_changes"
    assert result["score"] == 35
    assert len(result["comments"]) == 1
    assert result["comments"][0]["severity"] == "error"


@pytest.mark.asyncio
async def test_strips_markdown_fences():
    fenced = '```json\n{"summary":"ok","verdict":"approve","score":90,"comments":[]}\n```'

    result = await AIReviewer(StubBackend(fenced)).review(CFG, "PR", "", DIFFS)

    assert result["verdict"] == "approve"
    assert result["score"] == 90


@pytest.mark.asyncio
async def test_request_carries_config_model_and_timeout():
    backend = StubBackend(review_json())
    cfg = AIReviewConfig(enabled=True, model="local-model:7b", timeout_seconds=120)

    await AIReviewer(backend).review(cfg, "PR", "", DIFFS)

    assert backend.requests[0].model == "local-model:7b"
    assert backend.requests[0].timeout_seconds == 120


@pytest.mark.asyncio
async def test_prompt_contains_the_diff():
    backend = StubBackend(review_json())

    await AIReviewer(backend).review(CFG, "feat: auth", "Adds login", DIFFS)

    assert "src/auth.py" in backend.requests[0].prompt
    assert "admin123" in backend.requests[0].prompt


# ── Failure handling ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_graceful_fallback_on_backend_error():
    backend = StubBackend(*[BackendError("network")] * 3)

    result = await AIReviewer(backend).review(CFG, "PR", "", DIFFS)

    assert result["verdict"] == "comment"
    assert result["comments"] == []
    assert result["score"] == 50


@pytest.mark.asyncio
async def test_transient_failures_are_retried():
    backend = StubBackend(BackendError("429"), review_json())

    result = await AIReviewer(backend).review(CFG, "PR", "", DIFFS)

    assert result["score"] == 35
    assert len(backend.requests) == 2


@pytest.mark.asyncio
async def test_retry_count_is_configurable():
    cfg = AIReviewConfig(enabled=True, max_retries=4)
    backend = StubBackend(*[BackendError("boom")] * 5)

    await AIReviewer(backend).review(cfg, "PR", "", DIFFS)

    assert len(backend.requests) == 5


@pytest.mark.asyncio
async def test_zero_retries_means_one_attempt():
    cfg = AIReviewConfig(enabled=True, max_retries=0)
    backend = StubBackend(BackendError("boom"))

    await AIReviewer(backend).review(cfg, "PR", "", DIFFS)

    assert len(backend.requests) == 1


@pytest.mark.asyncio
async def test_misconfiguration_is_not_retried():
    """A missing API key will not fix itself; retrying just delays the PR."""
    backend = StubBackend(*[BackendUnavailable("no key")] * 3)

    result = await AIReviewer(backend).review(CFG, "PR", "", DIFFS)

    assert result["score"] == 50
    assert len(backend.requests) == 1


@pytest.mark.asyncio
async def test_unexpected_errors_do_not_escape():
    backend = StubBackend(RuntimeError("something odd"))

    result = await AIReviewer(backend).review(CFG, "PR", "", DIFFS)

    assert result["verdict"] == "comment"


@pytest.mark.asyncio
async def test_unparseable_response_degrades_gracefully():
    result = await AIReviewer(StubBackend("I'm afraid I can't do that")).review(
        CFG, "PR", "", DIFFS
    )

    assert result["verdict"] == "comment"
    assert result["comments"] == []


# ── Parsing ───────────────────────────────────────────────────


def test_parse_invalid_verdict_defaults_to_comment():
    r = AIReviewer(StubBackend())
    result = r._parse('{"summary":"x","verdict":"blah","score":50,"comments":[]}')
    assert result["verdict"] == "comment"


def test_parse_clamps_score():
    r = AIReviewer(StubBackend())
    result = r._parse('{"summary":"x","verdict":"approve","score":999,"comments":[]}')
    assert result["score"] == 100

    result2 = r._parse('{"summary":"x","verdict":"approve","score":-50,"comments":[]}')
    assert result2["score"] == 0


def test_parse_caps_comments_at_20():
    many = [{"path": f"f{i}.py", "line": i+1, "body": "issue", "severity": "info"}
            for i in range(30)]
    r = AIReviewer(StubBackend())
    result = r._parse(json.dumps({
        "summary": "many issues", "verdict": "request_changes",
        "score": 20, "comments": many
    }))
    assert len(result["comments"]) <= 20


def test_parse_filters_empty_comments():
    r = AIReviewer(StubBackend())
    result = r._parse(json.dumps({
        "summary": "ok", "verdict": "comment", "score": 50,
        "comments": [
            {"path": "", "line": 1, "body": "has no path", "severity": "info"},
            {"path": "real.py", "line": 1, "body": "", "severity": "info"},
            {"path": "real.py", "line": 2, "body": "valid comment", "severity": "warning"},
        ]
    }))
    assert len(result["comments"]) == 1
    assert result["comments"][0]["body"] == "valid comment"
