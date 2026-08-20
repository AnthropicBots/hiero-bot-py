import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.github.client import GitHubClient


@pytest.mark.asyncio
async def test_concurrent_token_refresh():
    client = GitHubClient()
    installation_id = 12345

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = Mock()
    mock_response.json = Mock(
        return_value={
            "token": "test_token_123",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    )

    with (
        patch.object(client, "_make_jwt", return_value="fake_jwt"),
        patch.object(client._http, "post", return_value=mock_response) as mock_post,
    ):
        tokens = await asyncio.gather(
            *[
                client._installation_token(installation_id)
                for _ in range(5)
            ]
        )

        assert all(t == "test_token_123" for t in tokens)
        assert mock_post.call_count == 1

    await client.close()


@pytest.mark.asyncio
async def test_installation_token_uses_expires_at():
    client = GitHubClient()

    mock_response = AsyncMock()
    mock_response.raise_for_status = Mock()
    mock_response.json = Mock(
        return_value={
            "token": "abc123",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    )

    with (
        patch.object(client, "_make_jwt", return_value="fake_jwt"),
        patch.object(client._http, "post", return_value=mock_response),
    ):
        token = await client._installation_token(1)

    assert token == "abc123"

    cached_token, expiry = client._installation_tokens[1]
    assert cached_token == "abc123"
    assert expiry > time.time()

    await client.close()


@pytest.mark.asyncio
async def test_installation_token_fallback_to_one_hour():
    client = GitHubClient()

    mock_response = AsyncMock()
    mock_response.raise_for_status = Mock()
    mock_response.json = Mock(
        return_value={
            "token": "fallback_token",
            "expires_at": "invalid-date",
        }
    )

    before = time.time()

    with (
        patch.object(client, "_make_jwt", return_value="fake_jwt"),
        patch.object(client._http, "post", return_value=mock_response),
    ):
        await client._installation_token(2)

    _, expiry = client._installation_tokens[2]

    assert before + 3500 < expiry < before + 3700

    await client.close()


@pytest.mark.asyncio
async def test_installation_token_cache_hit():
    client = GitHubClient()

    client._installation_tokens[99] = (
        "cached_token",
        time.time() + 3600,
    )

    with patch.object(client._http, "post") as mock_post:
        token = await client._installation_token(99)

    assert token == "cached_token"
    mock_post.assert_not_called()

    await client.close()

@pytest.mark.asyncio
async def test_list_issue_comments_paginates():
    client = GitHubClient()

    first_page = [
        {"id": i, "body": f"Comment {i}"}
        for i in range(100)
    ]
    second_page = [
        {
            "id": 12345,
            "body": "## 🔍 Quality Gate Report\n\nPrevious report",
        }
    ]

    with patch.object(
        client,
        "get",
        new=AsyncMock(side_effect=[first_page, second_page]),
    ) as mock_get:
        comments = await client.list_issue_comments(
            "hiero",
            "sdk-js",
            1,
            123,
        )

    assert len(comments) == 101
    assert comments[-1]["id"] == 12345

    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/issues/1/comments",
        123,
        params={"per_page": 100, "page": 1},
    )
    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/issues/1/comments",
        123,
        params={"per_page": 100, "page": 2},
    )
    assert mock_get.await_count == 2

    await client.close()

@pytest.mark.asyncio
async def test_list_commits_uses_path_and_per_page():
    client = GitHubClient()

    with patch.object(
        client,
        "get",
        new=AsyncMock(
            return_value=[
                {"sha": "abc123", "author": {"login": "bob"}},
            ]
        ),
    ) as mock_get:
        commits = await client.list_commits(
            "hiero",
            "sdk-js",
            123,
            path="app/github",
            per_page=30,
        )

    assert commits == [
        {"sha": "abc123", "author": {"login": "bob"}},
    ]

    mock_get.assert_awaited_once_with(
        "/repos/hiero/sdk-js/commits",
        123,
        params={
            "path": "app/github",
            "per_page": 30,
        },
    )

    await client.close()
