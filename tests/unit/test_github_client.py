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


# ── Pagination ────────────────────────────────────────────────


def page(size, start=0):
    return [{"number": start + i} for i in range(size)]


@pytest.mark.asyncio
async def test_paginate_stops_on_short_page():
    client = GitHubClient()

    with patch.object(
        client, "get", new=AsyncMock(side_effect=[page(100), page(7, 100)])
    ) as mock_get:
        items = await client.paginate("/repos/hiero/sdk-js/issues", 1)

    assert len(items) == 107
    assert mock_get.await_count == 2

    await client.close()


@pytest.mark.asyncio
async def test_paginate_single_short_page_makes_one_request():
    client = GitHubClient()

    with patch.object(client, "get", new=AsyncMock(return_value=page(3))) as mock_get:
        items = await client.paginate("/repos/hiero/sdk-js/issues", 1)

    assert len(items) == 3
    assert mock_get.await_count == 1

    await client.close()


@pytest.mark.asyncio
async def test_paginate_empty_first_page():
    client = GitHubClient()

    with patch.object(client, "get", new=AsyncMock(return_value=[])) as mock_get:
        items = await client.paginate("/repos/hiero/sdk-js/issues", 1)

    assert items == []
    assert mock_get.await_count == 1

    await client.close()


@pytest.mark.asyncio
async def test_paginate_respects_max_pages_cap():
    client = GitHubClient()

    with patch.object(client, "get", new=AsyncMock(return_value=page(100))) as mock_get:
        items = await client.paginate("/repos/hiero/sdk-js/issues", 1, max_pages=3)

    assert len(items) == 300
    assert mock_get.await_count == 3

    await client.close()


@pytest.mark.asyncio
async def test_paginate_preserves_caller_params():
    client = GitHubClient()

    with patch.object(client, "get", new=AsyncMock(return_value=page(2))) as mock_get:
        await client.paginate(
            "/repos/hiero/sdk-js/issues", 9, params={"state": "open"}
        )

    mock_get.assert_awaited_once_with(
        "/repos/hiero/sdk-js/issues",
        9,
        params={"state": "open", "per_page": 100, "page": 1},
    )

    await client.close()


@pytest.mark.asyncio
async def test_paginate_extracts_wrapped_collection():
    client = GitHubClient()

    responses = [
        {"total_count": 101, "repositories": page(100)},
        {"total_count": 101, "repositories": page(1, 100)},
    ]

    with patch.object(client, "get", new=AsyncMock(side_effect=responses)):
        repos = await client.paginate(
            "/installation/repositories", 5, extract="repositories"
        )

    assert len(repos) == 101

    await client.close()


@pytest.mark.asyncio
async def test_paginate_breaks_on_unexpected_payload():
    client = GitHubClient()

    with patch.object(client, "get", new=AsyncMock(return_value={"message": "nope"})):
        items = await client.paginate("/repos/hiero/sdk-js/issues", 1)

    assert items == []

    await client.close()


@pytest.mark.asyncio
async def test_list_issues_walks_every_page():
    """Regression for #46 — repos with >100 open issues got partial stale scans."""
    client = GitHubClient()

    with patch.object(
        client, "get", new=AsyncMock(side_effect=[page(100), page(100, 100), page(5, 200)])
    ) as mock_get:
        issues = await client.list_issues(
            "hiero", "sdk-js", 42, state="open", sort="updated"
        )

    assert len(issues) == 205
    assert mock_get.await_count == 3
    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/issues",
        42,
        params={"state": "open", "sort": "updated", "per_page": 100, "page": 3},
    )

    await client.close()


@pytest.mark.asyncio
async def test_list_installation_repos_unwraps_and_paginates():
    client = GitHubClient()

    responses = [
        {"repositories": [{"full_name": f"hiero/repo-{i}"} for i in range(100)]},
        {"repositories": [{"full_name": "hiero/last"}]},
    ]

    with patch.object(client, "get", new=AsyncMock(side_effect=responses)):
        repos = await client.list_installation_repos(7)

    assert len(repos) == 101
    assert repos[-1]["full_name"] == "hiero/last"

    await client.close()


@pytest.mark.asyncio
async def test_list_installations_paginates_with_app_auth():
    client = GitHubClient()

    first = Mock()
    first.raise_for_status = Mock()
    first.json = Mock(return_value=[{"id": i} for i in range(100)])

    second = Mock()
    second.raise_for_status = Mock()
    second.json = Mock(return_value=[{"id": 100}])

    with (
        patch.object(client, "_make_jwt", return_value="fake_jwt"),
        patch.object(
            client._http, "get", new=AsyncMock(side_effect=[first, second])
        ) as mock_get,
    ):
        installations = await client.list_installations()

    assert len(installations) == 101
    assert mock_get.await_count == 2

    await client.close()
