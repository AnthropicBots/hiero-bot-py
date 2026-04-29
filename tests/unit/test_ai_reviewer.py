# tests/unit/test_ai_reviewer.py

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.ai.reviewer import AIReviewer
from app.config.schema import AIReviewConfig


CFG = AIReviewConfig(enabled=True, max_comments=5, focus_areas=["security", "logic"])
DIFFS = [{"path": "src/auth.py", "diff": "+password = 'admin123'"}]


def make_response(data: dict):
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=json.dumps(data))]
    return mock_msg


@pytest.mark.asyncio
async def test_raises_when_disabled():
    disabled = AIReviewConfig(enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        await AIReviewer().review(disabled, "title", "body", DIFFS)


@pytest.mark.asyncio
async def test_returns_parsed_review():
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=make_response({
        "summary": "Hardcoded credential found.",
        "verdict": "request_changes",
        "score": 35,
        "comments": [
            {"path": "src/auth.py", "line": 1, "body": "Never hardcode passwords.", "severity": "error"}
        ],
    }))
    r = AIReviewer()
    r._client = mock_client
    result = await r.review(CFG, "feat: auth", "Adds login", DIFFS)
    assert result["verdict"] == "request_changes"
    assert result["score"] == 35
    assert len(result["comments"]) == 1
    assert result["comments"][0]["severity"] == "error"


@pytest.mark.asyncio
async def test_graceful_fallback_on_api_error():
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("Network error"))
    r = AIReviewer()
    r._client = mock_client
    result = await r.review(CFG, "PR", "", DIFFS)
    assert result["verdict"] == "comment"
    assert result["comments"] == []
    assert result["score"] == 50


@pytest.mark.asyncio
async def test_strips_markdown_fences():
    mock_client = AsyncMock()
    text = '```json\n{"summary":"ok","verdict":"approve","score":90,"comments":[]}\n```'
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=text)]
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    r = AIReviewer()
    r._client = mock_client
    result = await r.review(CFG, "PR", "", DIFFS)
    assert result["verdict"] == "approve"
    assert result["score"] == 90


def test_parse_invalid_verdict_defaults_to_comment():
    r = AIReviewer()
    result = r._parse('{"summary":"x","verdict":"blah","score":50,"comments":[]}')
    assert result["verdict"] == "comment"


def test_parse_clamps_score():
    r = AIReviewer()
    result = r._parse('{"summary":"x","verdict":"approve","score":999,"comments":[]}')
    assert result["score"] == 100

    result2 = r._parse('{"summary":"x","verdict":"approve","score":-50,"comments":[]}')
    assert result2["score"] == 0


def test_parse_caps_comments_at_20():
    many = [{"path": f"f{i}.py", "line": i+1, "body": "issue", "severity": "info"}
            for i in range(30)]
    r = AIReviewer()
    result = r._parse(json.dumps({
        "summary": "many issues", "verdict": "request_changes",
        "score": 20, "comments": many
    }))
    assert len(result["comments"]) <= 20


def test_parse_filters_empty_comments():
    r = AIReviewer()
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
