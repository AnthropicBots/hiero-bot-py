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
async def test_list_pr_files_paginates():
    client = GitHubClient()

    first_page = [{"filename": f"file_{i}.py"} for i in range(100)]
    second_page = [{"filename": "file_100.py"}]

    with patch.object(
        client,
        "get",
        new=AsyncMock(side_effect=[first_page, second_page]),
    ) as mock_get:
        files = await client.list_pr_files(
            owner="AnthropicBots",
            repo="hiero-bot-py",
            pr_number=79,
            installation_id=123,
        )

    assert len(files) == 101
    assert files[-1]["filename"] == "file_100.py"

    mock_get.assert_any_await(
        "/repos/AnthropicBots/hiero-bot-py/pulls/79/files",
        123,
        params={"per_page": 100, "page": 1},
    )
    mock_get.assert_any_await(
        "/repos/AnthropicBots/hiero-bot-py/pulls/79/files",
        123,
        params={"per_page": 100, "page": 2},
    )
    assert mock_get.await_count == 2

    await client.close()


@pytest.mark.asyncio
async def test_list_pr_reviews_paginates():
    client = GitHubClient()

    first_page = [
        {"id": i, "user": {"login": "alice"}, "state": "APPROVED"}
        for i in range(100)
    ]
    second_page = [
        {"id": 100, "user": {"login": "alice"}, "state": "CHANGES_REQUESTED"}
    ]

    with patch.object(
        client,
        "get",
        new=AsyncMock(side_effect=[first_page, second_page]),
    ) as mock_get:
        reviews = await client.list_pr_reviews("hiero", "sdk-js", 42, 123)

    assert len(reviews) == 101
    assert reviews[-1]["id"] == 100

    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/pulls/42/reviews",
        123,
        params={"per_page": 100, "page": 1},
    )
    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/pulls/42/reviews",
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
async def test_count_assigned_open_issues_single_page():
    client = GitHubClient()

    items = [
        {"number": 1},
        {"number": 2},
        {"number": 3},
    ]

    with patch.object(
        client,
        "get",
        new=AsyncMock(return_value=items),
    ) as mock_get:
        count = await client.count_assigned_open_issues(
            "hiero",
            "sdk-js",
            "alice",
            123,
        )

    assert count == 3
    mock_get.assert_awaited_once_with(
        "/repos/hiero/sdk-js/issues",
        123,
        params={
            "assignee": "alice",
            "state": "open",
            "per_page": 100,
            "page": 1,
        },
    )

    await client.close()


@pytest.mark.asyncio
async def test_count_assigned_open_issues_paginates():
    client = GitHubClient()

    first_page = [{"number": i} for i in range(100)]
    second_page = [{"number": 100 + i} for i in range(25)]

    with patch.object(
        client,
        "get",
        new=AsyncMock(side_effect=[first_page, second_page]),
    ) as mock_get:
        count = await client.count_assigned_open_issues(
            "hiero",
            "sdk-js",
            "alice",
            123,
        )

    assert count == 125
    assert mock_get.await_count == 2

    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/issues",
        123,
        params={
            "assignee": "alice",
            "state": "open",
            "per_page": 100,
            "page": 1,
        },
    )
    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/issues",
        123,
        params={
            "assignee": "alice",
            "state": "open",
            "per_page": 100,
            "page": 2,
        },
    )

    await client.close()


@pytest.mark.asyncio
async def test_count_assigned_open_issues_excludes_pull_requests():
    client = GitHubClient()

    items = [
        {"number": 1},
        {"number": 2, "pull_request": {"url": "https://api.github.com/pr/2"}},
        {"number": 3},
        {"number": 4, "pull_request": {"url": "https://api.github.com/pr/4"}},
    ]

    with patch.object(
        client,
        "get",
        new=AsyncMock(return_value=items),
    ):
        count = await client.count_assigned_open_issues(
            "hiero",
            "sdk-js",
            "alice",
            123,
        )

    assert count == 2

    await client.close()


@pytest.mark.asyncio
async def test_count_assigned_open_issues_respects_max_pages():
    client = GitHubClient()

    first_page = [{"number": i} for i in range(100)]
    second_page = [{"number": 100 + i} for i in range(100)]
    third_page = [{"number": 200 + i} for i in range(100)]
    fourth_page = [{"number": 300 + i} for i in range(100)]

    with patch.object(
        client,
        "get",
        new=AsyncMock(
            side_effect=[
                first_page,
                second_page,
                third_page,
                fourth_page,
            ]
        ),
    ) as mock_get:
        count = await client.count_assigned_open_issues(
            "hiero",
            "sdk-js",
            "alice",
            123,
            max_pages=3,
        )

    assert count == 300
    assert mock_get.await_count == 3

    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/issues",
        123,
        params={
            "assignee": "alice",
            "state": "open",
            "per_page": 100,
            "page": 3,
        },
    )

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

    with patch.object(
        client, "get", new=AsyncMock(return_value={"message": "nope"})
    ):
        items = await client.paginate("/repos/hiero/sdk-js/issues", 1)

    assert items == []

    await client.close()


@pytest.mark.asyncio
async def test_list_issues_walks_every_page():
    """Regression for #46 — repos with >100 open issues got partial stale scans."""
    client = GitHubClient()

    with patch.object(
        client,
        "get",
        new=AsyncMock(side_effect=[page(100), page(100, 100), page(5, 200)]),
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
    first.status_code = 200
    first.raise_for_status = Mock()
    first.headers = {}
    first.json = Mock(return_value=[{"id": i} for i in range(100)])

    second = Mock()
    second.status_code = 200
    second.raise_for_status = Mock()
    second.headers = {}
    second.json = Mock(return_value=[{"id": 100}])

    with (
        patch.object(client, "_make_jwt", return_value="fake_jwt"),
        patch.object(
            client._http,
            "get",
            new=AsyncMock(side_effect=[first, second]),
        ) as mock_get,
    ):
        installations = await client.list_installations()

    assert len(installations) == 101
    assert mock_get.await_count == 2

    mock_get.assert_any_await(
        "/app/installations",
        headers={"Authorization": "Bearer fake_jwt"},
        params={"per_page": 100, "page": 1},
    )
    mock_get.assert_any_await(
        "/app/installations",
        headers={"Authorization": "Bearer fake_jwt"},
        params={"per_page": 100, "page": 2},
    )

    await client.close()


@pytest.mark.asyncio
async def test_search_issues_passes_query_pagination_and_sort_params():
    client = GitHubClient()

    with patch.object(
        client,
        "get",
        new=AsyncMock(
            return_value={
                "total_count": 137,
                "items": [],
            }
        ),
    ) as mock_get:
        result = await client.search_issues(
            "repo:hiero/sdk-js type:pr author:alice is:merged",
            42,
            per_page=100,
            page=2,
            sort="created",
            order="asc",
        )

    assert result == {
        "total_count": 137,
        "items": [],
    }

    mock_get.assert_awaited_once_with(
        "/search/issues",
        42,
        params={
            "q": "repo:hiero/sdk-js type:pr author:alice is:merged",
            "per_page": 100,
            "page": 2,
            "sort": "created",
            "order": "asc",
        },
    )

    await client.close()


@pytest.mark.asyncio
async def test_paginate_search_walks_every_page():
    client = GitHubClient()

    responses = [
        {
            "total_count": 101,
            "items": [{"number": i} for i in range(100)],
        },
        {
            "total_count": 101,
            "items": [{"number": 100}],
        },
    ]

    with patch.object(
        client,
        "search_issues",
        new=AsyncMock(side_effect=responses),
    ) as mock_search:
        items = await client.paginate_search(
            "repo:hiero/sdk-js type:pr reviewed-by:alice",
            42,
        )

    assert len(items) == 101
    assert items[-1]["number"] == 100
    assert mock_search.await_count == 2

    first_call = mock_search.await_args_list[0]
    second_call = mock_search.await_args_list[1]

    assert first_call.args == (
        "repo:hiero/sdk-js type:pr reviewed-by:alice",
        42,
    )
    assert first_call.kwargs == {
        "per_page": 100,
        "page": 1,
        "sort": None,
        "order": None,
    }

    assert second_call.args == (
        "repo:hiero/sdk-js type:pr reviewed-by:alice",
        42,
    )
    assert second_call.kwargs == {
        "per_page": 100,
        "page": 2,
        "sort": None,
        "order": None,
    }

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
