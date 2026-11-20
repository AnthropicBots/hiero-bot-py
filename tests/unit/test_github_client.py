import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
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
async def test_list_installations_retries_secondary_limit_with_wait_cap():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/app/installations")
    responses = [
        httpx.Response(
            403,
            headers={"Retry-After": "2000"},
            json={"message": "secondary rate limit"},
            request=request,
        )
        for _ in range(2)
    ]
    with (
        patch.object(client, "_make_jwt", return_value="fake_jwt"),
        patch.object(
            client._http, "get", new=AsyncMock(side_effect=responses)
        ) as app_get,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.list_installations()
    assert app_get.await_count == 2
    sleep.assert_awaited_once_with(2000.0)
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


@pytest.mark.asyncio
async def test_remove_label_deletes_the_issue_label():
    """Issue #64 — GitHubClient had no way to remove a label at all."""
    client = GitHubClient()

    with patch.object(
        client, "delete", new=AsyncMock(return_value={})
    ) as mock_delete:
        await client.remove_label(
            "hiero", "sdk-js", 7, "health: 🔧 needs work", 123
        )

    mock_delete.assert_awaited_once_with(
        "/repos/hiero/sdk-js/issues/7/labels/health%3A%20%F0%9F%94%A7%20needs%20work",
        123,
    )

    await client.close()


@pytest.mark.asyncio
async def test_remove_label_tolerates_label_already_absent():
    """A label that isn't currently applied (404 from GitHub) must not raise."""
    client = GitHubClient()

    with patch.object(
        client,
        "delete",
        new=AsyncMock(side_effect=httpx.HTTPStatusError(
            "Not found", request=Mock(), response=Mock(status_code=404)
        )),
    ) as mock_delete:
        # Should not raise
        await client.remove_label("hiero", "sdk-js", 7, "quality: ✅ passed", 123)

    mock_delete.assert_awaited_once()

    await client.close()


@pytest.mark.asyncio
async def test_remove_label_propagates_non_404_errors():
    """403 and other errors must not be silently ignored."""
    client = GitHubClient()

    forbidden_error = httpx.HTTPStatusError(
        "Forbidden",
        request=Mock(),
        response=Mock(status_code=403),
    )

    with (
            patch.object(
                client,
                "delete",
                new=AsyncMock(side_effect=forbidden_error),
            ),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            await client.remove_label(
                "hiero", "sdk-js", 7, "quality: passed", 123
            )

    assert exc_info.value.response.status_code == 403

    await client.close()


@pytest.mark.asyncio
async def test_list_labels_paginates():
    client = GitHubClient()

    first_page = [{"name": f"label_{i}"} for i in range(100)]
    second_page = [{"name": "label_100"}]

    with patch.object(
        client,
        "get",
        new=AsyncMock(side_effect=[first_page, second_page]),
    ) as mock_get:
        labels = await client.list_labels("hiero", "sdk-js", 123)

    assert len(labels) == 101
    assert labels[-1]["name"] == "label_100"

    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/labels",
        123,
        params={"per_page": 100, "page": 1},
    )
    mock_get.assert_any_await(
        "/repos/hiero/sdk-js/labels",
        123,
        params={"per_page": 100, "page": 2},
    )

    await client.close()


@pytest.mark.asyncio
async def test_add_label_creates_missing_label_on_404():
    client = GitHubClient()

    not_found_err = httpx.HTTPStatusError(
        "Not found", request=Mock(), response=Mock(status_code=404)
    )

    with (
        patch.object(client, "get", new=AsyncMock(side_effect=not_found_err)) as mock_get,
        patch.object(client, "post", new=AsyncMock(return_value={})) as mock_post,
    ):
        await client.add_label("hiero", "sdk-js", 7, "action: merge", 123)

    mock_get.assert_awaited_once_with(
        "/repos/hiero/sdk-js/labels/action%3A%20merge", 123
    )
    mock_post.assert_any_await(
        "/repos/hiero/sdk-js/labels",
        123,
        json={"name": "action: merge", "color": "ededed"},
    )
    mock_post.assert_any_await(
        "/repos/hiero/sdk-js/issues/7/labels",
        123,
        json={"labels": ["action: merge"]},
    )
    assert mock_post.await_count == 2

    await client.close()


@pytest.mark.asyncio
async def test_add_label_existing_label_does_not_create():
    client = GitHubClient()

    with (
        patch.object(client, "get", new=AsyncMock(return_value={"name": "bug"})) as mock_get,
        patch.object(client, "post", new=AsyncMock(return_value={})) as mock_post,
    ):
        await client.add_label("hiero", "sdk-js", 7, "bug", 123)

    mock_get.assert_awaited_once_with("/repos/hiero/sdk-js/labels/bug", 123)
    mock_post.assert_awaited_once_with(
        "/repos/hiero/sdk-js/issues/7/labels",
        123,
        json={"labels": ["bug"]},
    )

    await client.close()


@pytest.mark.asyncio
async def test_add_label_non_404_error_raises_and_does_not_create():
    client = GitHubClient()

    server_err = httpx.HTTPStatusError(
        "Internal Server Error", request=Mock(), response=Mock(status_code=500)
    )

    with (
        patch.object(client, "get", new=AsyncMock(side_effect=server_err)) as mock_get,
        patch.object(client, "post", new=AsyncMock(return_value={})) as mock_post,
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await client.add_label("hiero", "sdk-js", 7, "bug", 123)

    assert exc_info.value.response.status_code == 500
    mock_get.assert_awaited_once_with("/repos/hiero/sdk-js/labels/bug", 123)
    mock_post.assert_not_called()

    await client.close()


@pytest.mark.asyncio
async def test_add_label_create_if_missing_false_never_creates():
    client = GitHubClient()

    with (
        patch.object(client, "get", new=AsyncMock()) as mock_get,
        patch.object(client, "post", new=AsyncMock(return_value={})) as mock_post,
    ):
        await client.add_label(
            "hiero", "sdk-js", 7, "action: merge", 123, create_if_missing=False
        )

    mock_get.assert_not_called()
    mock_post.assert_awaited_once_with(
        "/repos/hiero/sdk-js/issues/7/labels",
        123,
        json={"labels": ["action: merge"]},
    )

    await client.close()


@pytest.mark.asyncio
async def test_remove_label_url_encodes_slashes():
    client = GitHubClient()
    with patch.object(client, "delete", new=AsyncMock(return_value={})) as mock_delete:
        await client.remove_label("hiero", "sdk-js", 7, "kind/bug", 123)
    mock_delete.assert_awaited_once_with(
        "/repos/hiero/sdk-js/issues/7/labels/kind%2Fbug", 123
    )
    await client.close()


@pytest.mark.asyncio
async def test_add_label_treats_already_exists_as_success():
    client = GitHubClient()
    exists_error = httpx.HTTPStatusError(
        "Unprocessable Entity",
        request=Mock(),
        response=httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"code": "already_exists"}],
            },
            request=httpx.Request(
                "POST", "https://api.github.com/repos/hiero/sdk-js/labels"
            ),
        ),
    )
    with (
        patch.object(
            client,
            "get",
            new=AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "Not found", request=Mock(), response=Mock(status_code=404)
                )
            ),
        ) as mock_get,
        patch.object(
            client, "post", new=AsyncMock(side_effect=[exists_error, {}])
        ) as mock_post,
    ):
        await client.add_label("hiero", "sdk-js", 7, "kind/bug", 123)
    mock_get.assert_awaited_once_with(
        "/repos/hiero/sdk-js/labels/kind%2Fbug", 123
    )
    assert mock_post.await_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_request_respects_retry_after_without_capping_wait():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(
            403,
            headers={"Retry-After": "999"},
            json={"message": "secondary rate limit"},
            request=request,
        ),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(client._http, "request", new=AsyncMock(side_effect=responses)),
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    sleep.assert_awaited_once_with(999.0)
    await client.close()


@pytest.mark.asyncio
async def test_request_does_not_retry_post_after_read_timeout():
    client = GitHubClient()
    request_error = httpx.ReadTimeout("timed out")
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=request_error)
        ) as request,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(httpx.ReadTimeout),
    ):
        await client.request(
            "POST",
            "/repos/hiero/sdk-js/issues/1/comments",
            123,
            json={"body": "hello"},
        )
    request.assert_awaited_once()
    sleep.assert_not_awaited()
    await client.close()


@pytest.mark.asyncio
async def test_request_does_not_retry_post_after_network_error():
    client = GitHubClient()
    request_error = httpx.ConnectError("network unavailable")
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=request_error)
        ) as request,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(httpx.ConnectError),
    ):
        await client.request(
            "POST",
            "/repos/hiero/sdk-js/issues/1/comments",
            123,
            json={"body": "hello"},
        )
    request.assert_awaited_once()
    sleep.assert_not_awaited()
    await client.close()


@pytest.mark.parametrize(
    ("status_code", "expected_wait"),
    [(429, 60.0), (502, 1.0), (503, 1.0), (504, 1.0)],
)
@pytest.mark.asyncio
async def test_request_retries_rate_limit_and_gateway_statuses(
    status_code, expected_wait
):
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(status_code, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=responses)
        ) as request_mock,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    assert request_mock.await_count == 2
    sleep.assert_awaited_once_with(expected_wait)
    await client.close()


@pytest.mark.asyncio
async def test_request_caps_total_retry_wait_without_retrying_early():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(
            403,
            headers={"Retry-After": "2000"},
            json={"message": "secondary rate limit"},
            request=request,
        )
        for _ in range(2)
    ]
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=responses)
        ) as request_mock,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert request_mock.await_count == 2
    sleep.assert_awaited_once_with(2000.0)
    await client.close()


@pytest.mark.asyncio
async def test_request_invalidates_revoked_installation_token_and_retries_once():
    client = GitHubClient()
    client._installation_tokens[123] = ("revoked-token", time.time() + 3600)
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(401, json={"message": "Bad credentials"}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(
                side_effect=[
                    {"Authorization": "token revoked-token"},
                    {"Authorization": "token refreshed"},
                ]
            ),
        ) as headers,
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=responses)
        ) as request_mock,
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    assert 123 not in client._installation_tokens
    assert headers.await_count == 2
    assert request_mock.await_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_request_does_not_refresh_token_more_than_once():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(401, json={"message": "Bad credentials"}, request=request),
        httpx.Response(401, json={"message": "Bad credentials"}, request=request),
    ]
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(
                side_effect=[
                    {"Authorization": "Bearer old-token"},
                    {"Authorization": "Bearer fresh-token"},
                ]
            ),
        ) as headers,
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=responses)
        ) as request_mock,
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert headers.await_count == 2
    assert request_mock.await_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_request_does_not_retry_post_after_server_error():
    client = GitHubClient()
    request = httpx.Request(
        "POST", "https://api.github.com/repos/hiero/sdk-js/issues/1/labels"
    )
    response = httpx.Response(502, json={"message": "bad gateway"}, request=request)
    with (
        patch.object(
            client,
            "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(client._http, "request", new=AsyncMock(return_value=response)) as request_mock,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.request(
            "POST",
            "/repos/hiero/sdk-js/issues/1/labels",
            123,
            json={"labels": ["bug"]},
        )
    request_mock.assert_awaited_once()
    sleep.assert_not_awaited()
    await client.close()


@pytest.mark.asyncio
async def test_request_retries_post_after_explicit_secondary_rate_limit():
    client = GitHubClient()
    request = httpx.Request(
        "POST", "https://api.github.com/repos/hiero/sdk-js/issues/1/comments"
    )
    responses = [
        httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit"},
            request=request,
        ),
        httpx.Response(200, json={"id": 42}, request=request),
    ]
    with (
        patch.object(
            client, "_inst_headers",
            new=AsyncMock(return_value={"Authorization": "token"}),
        ),
        patch.object(
            client._http, "request", new=AsyncMock(side_effect=responses)
        ) as request_mock,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        result = await client.request(
            "POST",
            "/repos/hiero/sdk-js/issues/1/comments",
            123,
            json={"body": "hello"},
        )
    assert result == {"id": 42}
    assert request_mock.await_count == 2
    sleep.assert_awaited_once_with(60.0)
    await client.close()


@pytest.mark.asyncio
async def test_secondary_rate_limit_without_retry_after_waits_at_least_one_minute():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(
            403, json={"message": "secondary rate limit"}, request=request
        ),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(client, "_inst_headers",
                     new=AsyncMock(return_value={"Authorization": "Bearer token"})),
        patch.object(client._http, "request",
                     new=AsyncMock(side_effect=responses)),
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    sleep.assert_awaited_once_with(60.0)
    await client.close()


@pytest.mark.asyncio
async def test_primary_rate_limit_waits_until_reset():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    reset_at = time.time() + 120
    responses = [
        httpx.Response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_at),
            },
            json={"message": "API rate limit exceeded"},
            request=request,
        ),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(client, "_inst_headers",
                     new=AsyncMock(return_value={"Authorization": "Bearer token"})),
        patch.object(client._http, "request",
                     new=AsyncMock(side_effect=responses)),
        patch("app.github.client.asyncio.sleep", new=AsyncMock()) as sleep,
        patch("app.github.client.time.time", return_value=reset_at - 120),
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    sleep.assert_awaited_once_with(pytest.approx(120.0))
    await client.close()


@pytest.mark.asyncio
async def test_401_on_last_retry_still_sends_request_with_refreshed_token():
    client = GitHubClient()
    client._installation_tokens[123] = ("old-token", time.time() + 3600)
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(503, request=request),
        httpx.Response(503, request=request),
        httpx.Response(503, request=request),
        httpx.Response(401, json={"message": "Bad credentials"}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(client, "_inst_headers", new=AsyncMock(side_effect=[
            {"Authorization": "Bearer old-token"},
            {"Authorization": "Bearer new-token"},
        ])),
        patch.object(client._http, "request",
                     new=AsyncMock(side_effect=responses)) as api_request,
        patch("app.github.client.asyncio.sleep", new=AsyncMock()),
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    assert api_request.await_count == 5
    assert api_request.await_args_list[-1].kwargs["headers"] == {
        "Authorization": "Bearer new-token"
    }
    await client.close()


@pytest.mark.asyncio
async def test_401_does_not_remove_a_newer_concurrently_refreshed_token():
    client = GitHubClient()
    client._installation_tokens[123] = ("new-token", time.time() + 3600)
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    responses = [
        httpx.Response(401, json={"message": "Bad credentials"}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(client, "_inst_headers", new=AsyncMock(side_effect=[
            {"Authorization": "Bearer old-token"},
            {"Authorization": "Bearer new-token"},
        ])),
        patch.object(client._http, "request",
                     new=AsyncMock(side_effect=responses)),
    ):
        result = await client.request("GET", "/repos/hiero/sdk-js", 123)
    assert result == {"ok": True}
    assert client._installation_tokens[123][0] == "new-token"
    await client.close()


@pytest.mark.asyncio
async def test_token_invalidation_waits_for_refresh_lock():
    client = GitHubClient()
    installation_id = 123
    lock = client._refresh_locks.setdefault(installation_id, asyncio.Lock())
    client._installation_tokens[installation_id] = (
        "failed-token", time.time() + 3600
    )

    async with lock:
        invalidation = asyncio.create_task(
            client._invalidate_installation_token(
                installation_id, "failed-token"
            )
        )
        await asyncio.sleep(0)
        assert not invalidation.done()
        client._installation_tokens[installation_id] = (
            "fresh-token", time.time() + 3600
        )

    await invalidation
    assert client._installation_tokens[installation_id][0] == "fresh-token"
    await client.close()


@pytest.mark.asyncio
async def test_401_refresh_fetches_and_reuses_real_installation_token_cache():
    client = GitHubClient()
    request = httpx.Request("GET", "https://api.github.com/repos/hiero/sdk-js")
    token_request = httpx.Request(
        "POST", "https://api.github.com/app/installations/123/access_tokens"
    )
    token_response = httpx.Response(
        201,
        json={
            "token": "fresh-installation-token",
            "expires_at": "2030-01-01T00:00:00Z",
        },
        request=token_request,
    )
    api_responses = [
        httpx.Response(401, json={"message": "Bad credentials"}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]
    with (
        patch.object(client, "_make_jwt", return_value="test-app-jwt"),
        patch.object(client._http, "post",
                     new=AsyncMock(return_value=token_response)) as token_post,
        patch.object(client._http, "request",
                     new=AsyncMock(side_effect=api_responses)) as api_request,
    ):
        client._installation_tokens[123] = (
            "revoked-token", time.time() + 3600
        )
        first = await client.request("GET", "/repos/hiero/sdk-js", 123)
        second = await client.request("GET", "/repos/hiero/sdk-js", 123)

    assert first == {"ok": True}
    assert second == {"ok": True}
    token_post.assert_awaited_once_with(
        "/app/installations/123/access_tokens",
        headers={"Authorization": "Bearer test-app-jwt"},
    )
    assert client._installation_tokens[123][0] == "fresh-installation-token"
    assert api_request.await_args_list[1].kwargs["headers"] == {
        "Authorization": "Bearer fresh-installation-token"
    }
    assert api_request.await_args_list[2].kwargs["headers"] == {
        "Authorization": "Bearer fresh-installation-token"
    }
    await client.close()
