# tests/unit/test_forge.py — platform-neutral forge adapters

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.forge import (
    ForgeAuthError,
    ForgeError,
    ForgeNotFound,
    GitHubForge,
    GitLabForge,
    create_forge,
)
from app.forge.gitlab import project_id
from app.forge.models import parse_timestamp
from app.utils.settings import settings

# ── Shared fixtures ───────────────────────────────────────────


@pytest.fixture
def gh_client():
    client = AsyncMock()
    client.get_file_content = AsyncMock(return_value=None)
    client.list_issues = AsyncMock(return_value=[])
    client.list_pr_files = AsyncMock(return_value=[])
    client.list_issue_comments = AsyncMock(return_value=[])
    return client


@pytest.fixture
def github(gh_client):
    return GitHubForge(gh_client, installation_id=42)


def gitlab_forge(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="https://gitlab.com/api/v4",
        headers={"PRIVATE-TOKEN": "tok"},
        transport=transport,
    )
    return GitLabForge("tok", client=client)


# ── GitHub adapter ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_github_decodes_file_content(github, gh_client):
    import base64

    gh_client.get_file_content = AsyncMock(
        return_value=base64.b64encode(b"repo: hiero/sdk-js").decode()
    )

    assert await github.get_file("hiero", "sdk-js", ".github/x.yml") == (
        "repo: hiero/sdk-js"
    )


@pytest.mark.asyncio
async def test_github_missing_file_is_none(github):
    assert await github.get_file("hiero", "sdk-js", "nope.yml") is None


@pytest.mark.asyncio
async def test_github_issue_is_normalised(github, gh_client):
    gh_client.list_issues = AsyncMock(
        return_value=[
            {
                "number": 7,
                "title": "Broken thing",
                "state": "open",
                "user": {"login": "alice"},
                "html_url": "https://github.com/hiero/sdk-js/issues/7",
                "updated_at": "2026-01-02T03:04:05Z",
                "labels": [{"name": "bug"}, "stale"],
                "assignees": [{"login": "bob"}],
                "body": "details",
            }
        ]
    )

    issue = (await github.list_issues("hiero", "sdk-js"))[0]

    assert issue.number == 7
    assert issue.author == "alice"
    assert issue.labels == ("bug", "stale")
    assert issue.assignees == ("bob",)
    assert issue.is_pull_request is False
    assert issue.updated_at.year == 2026


@pytest.mark.asyncio
async def test_github_marks_pull_requests(github, gh_client):
    gh_client.list_issues = AsyncMock(
        return_value=[{"number": 1, "pull_request": {"url": "..."}}]
    )

    assert (await github.list_issues("hiero", "sdk-js"))[0].is_pull_request is True


@pytest.mark.asyncio
async def test_github_pull_request_is_normalised(github, gh_client):
    gh_client.get = AsyncMock(
        return_value={
            "number": 12,
            "title": "Add thing",
            "state": "closed",
            "user": {"login": "alice"},
            "head": {"ref": "feat/x", "sha": "abc123"},
            "base": {"ref": "main"},
            "merged_at": "2026-01-02T03:04:05Z",
            "draft": False,
            "changed_files": 4,
        }
    )

    pull = await github.get_pull_request("hiero", "sdk-js", 12)

    assert pull.source_branch == "feat/x"
    assert pull.target_branch == "main"
    assert pull.head_sha == "abc123"
    assert pull.merged is True
    assert pull.changed_files == 4


@pytest.mark.asyncio
async def test_github_files_are_normalised(github, gh_client):
    gh_client.list_pr_files = AsyncMock(
        return_value=[
            {"filename": "app/x.py", "status": "added", "additions": 3, "patch": "@@"}
        ]
    )

    changed = await github.list_pull_request_files("hiero", "sdk-js", 1)

    assert changed[0].path == "app/x.py"
    assert changed[0].status == "added"


@pytest.mark.asyncio
async def test_github_user_flags_bots(github, gh_client):
    gh_client.get_user = AsyncMock(
        return_value={"login": "dependabot[bot]", "type": "Bot", "public_repos": 0}
    )

    assert (await github.get_user("dependabot[bot]")).is_bot is True


@pytest.mark.asyncio
async def test_github_write_calls_thread_the_installation_id(github, gh_client):
    await github.post_comment("hiero", "sdk-js", 1, "hello")

    gh_client.post_comment.assert_awaited_once_with(
        "hiero", "sdk-js", 1, "hello", 42
    )


@pytest.mark.asyncio
async def test_github_empty_assignee_list_is_a_no_op(github, gh_client):
    await github.add_assignees("hiero", "sdk-js", 1, [])
    gh_client.add_assignees.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_404_becomes_forge_not_found(github, gh_client):
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(404, request=request)
    gh_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError("nope", request=request, response=response)
    )

    with pytest.raises(ForgeNotFound):
        await github.get_issue("hiero", "sdk-js", 1)


@pytest.mark.asyncio
async def test_github_other_http_errors_become_forge_error(github, gh_client):
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(500, request=request)
    gh_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError("boom", request=request, response=response)
    )

    with pytest.raises(ForgeError):
        await github.get_issue("hiero", "sdk-js", 1)


# ── GitLab adapter ────────────────────────────────────────────


def test_project_path_is_fully_url_encoded():
    """A raw slash here reads as a path separator and 404s."""
    assert project_id("hiero", "sdk-js") == "hiero%2Fsdk-js"
    assert project_id("group/sub", "repo") == "group%2Fsub%2Frepo"


def test_gitlab_requires_a_token():
    with pytest.raises(ForgeAuthError):
        GitLabForge("")


@pytest.mark.asyncio
async def test_gitlab_issues_map_iid_to_number():
    def handler(request):
        # raw_path, not path: httpx decodes %2F for display but sends it intact,
        # which is exactly what GitLab's encoded-project addressing needs.
        assert request.url.raw_path.startswith(
            b"/api/v4/projects/hiero%2Fsdk-js/issues"
        )
        assert request.url.params["state"] == "opened"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 99999,
                    "iid": 7,
                    "title": "Broken",
                    "state": "opened",
                    "author": {"username": "alice"},
                    "web_url": "https://gitlab.com/hiero/sdk-js/-/issues/7",
                    "labels": ["bug"],
                    "assignees": [{"username": "bob"}],
                    "description": "details",
                }
            ],
        )

    forge = gitlab_forge(handler)
    issue = (await forge.list_issues("hiero", "sdk-js"))[0]

    # The display number, not the global id — 99999 would target another project.
    assert issue.number == 7
    assert issue.state == "open"
    assert issue.author == "alice"
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_merge_request_is_normalised():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "iid": 12,
                "title": "Add thing",
                "state": "merged",
                "author": {"username": "alice"},
                "source_branch": "feat/x",
                "target_branch": "main",
                "sha": "abc123",
                "merged_at": "2026-01-02T03:04:05Z",
                "work_in_progress": True,
                "changes_count": 4,
            },
        )

    forge = gitlab_forge(handler)
    pull = await forge.get_pull_request("hiero", "sdk-js", 12)

    assert pull.number == 12
    assert pull.merged is True
    assert pull.draft is True
    assert pull.head_sha == "abc123"
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_changes_become_files():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "changes": [
                    {"new_path": "app/x.py", "new_file": True, "diff": "@@"},
                    {"old_path": "app/gone.py", "deleted_file": True},
                ]
            },
        )

    forge = gitlab_forge(handler)
    changed = await forge.list_pull_request_files("hiero", "sdk-js", 1)

    assert [f.path for f in changed] == ["app/x.py", "app/gone.py"]
    assert [f.status for f in changed] == ["added", "removed"]
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_system_notes_are_filtered_out():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"id": 1, "body": "real comment", "author": {"username": "alice"}},
                {"id": 2, "body": "assigned to @bob", "system": True,
                 "author": {"username": "alice"}},
            ],
        )

    forge = gitlab_forge(handler)
    comments = await forge.list_comments("hiero", "sdk-js", 1)

    assert [c.body for c in comments] == ["real comment"]
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_comment_posts_a_note():
    seen = {}

    def handler(request):
        seen["path"] = request.url.raw_path
        seen["body"] = request.content
        return httpx.Response(201, json={"id": 1})

    forge = gitlab_forge(handler)
    await forge.post_comment("hiero", "sdk-js", 5, "hello")

    assert seen["path"] == b"/api/v4/projects/hiero%2Fsdk-js/issues/5/notes"
    assert b"hello" in seen["body"]
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_label_is_added_via_issue_update():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(200, json={})

    forge = gitlab_forge(handler)
    await forge.add_label("hiero", "sdk-js", 5, "stale")

    assert seen["method"] == "PUT"
    assert b"add_labels" in seen["body"]
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_assignment_resolves_logins_to_ids():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v4/users":
            username = request.url.params["username"]
            return httpx.Response(200, json=[{"id": 501, "username": username}])
        if request.method == "GET":
            return httpx.Response(200, json={"iid": 5, "assignees": []})
        assert b"501" in request.content
        return httpx.Response(200, json={})

    forge = gitlab_forge(handler)
    await forge.add_assignees("hiero", "sdk-js", 5, ["alice"])

    assert ("GET", "/api/v4/users") in calls
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_unknown_login_is_skipped():
    def handler(request):
        if request.url.path == "/api/v4/users":
            return httpx.Response(200, json=[])
        raise AssertionError("must not attempt the update with no resolved ids")

    forge = gitlab_forge(handler)
    await forge.add_assignees("hiero", "sdk-js", 5, ["ghost"])
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_user_ids_are_cached():
    lookups = []

    def handler(request):
        if request.url.path == "/api/v4/users":
            lookups.append(request.url.params["username"])
            return httpx.Response(200, json=[{"id": 501, "username": "alice"}])
        if request.method == "GET":
            return httpx.Response(200, json={"iid": 5, "assignees": []})
        return httpx.Response(200, json={})

    forge = gitlab_forge(handler)
    await forge.add_assignees("hiero", "sdk-js", 5, ["alice"])
    await forge.add_assignees("hiero", "sdk-js", 6, ["alice"])

    assert lookups.count("alice") == 1
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_missing_file_is_none():
    forge = gitlab_forge(lambda request: httpx.Response(404))

    assert await forge.get_file("hiero", "sdk-js", "nope.yml") is None
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_401_becomes_auth_error():
    forge = gitlab_forge(lambda request: httpx.Response(401))

    with pytest.raises(ForgeAuthError):
        await forge.get_issue("hiero", "sdk-js", 1)
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_500_becomes_forge_error():
    forge = gitlab_forge(lambda request: httpx.Response(500, text="server down"))

    with pytest.raises(ForgeError):
        await forge.get_issue("hiero", "sdk-js", 1)
    await forge.close()


@pytest.mark.asyncio
async def test_gitlab_pagination_follows_pages():
    def handler(request):
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=[{"iid": i} for i in range(100)])
        return httpx.Response(200, json=[{"iid": 100}])

    forge = gitlab_forge(handler)
    issues = await forge.list_issues("hiero", "sdk-js")

    assert len(issues) == 101
    await forge.close()


# ── Factory ───────────────────────────────────────────────────


def test_factory_builds_github(gh_client):
    forge = create_forge("github", github_client=gh_client, installation_id=1)
    assert isinstance(forge, GitHubForge)


def test_factory_github_requires_a_client():
    with pytest.raises(ForgeError, match="requires a GitHubClient"):
        create_forge("github")


def test_factory_gitlab_requires_a_token(monkeypatch):
    monkeypatch.setattr(settings, "gitlab_token", None)
    with pytest.raises(ForgeError, match="GITLAB_TOKEN"):
        create_forge("gitlab")


def test_factory_builds_gitlab(monkeypatch):
    monkeypatch.setattr(settings, "gitlab_token", "tok")
    forge = create_forge("gitlab")

    assert isinstance(forge, GitLabForge)
    assert forge.name == "gitlab"


def test_factory_rejects_unknown_providers():
    with pytest.raises(ForgeError, match="Unknown forge provider"):
        create_forge("bitbucket")


def test_factory_defaults_to_the_configured_provider(monkeypatch, gh_client):
    monkeypatch.setattr(settings, "forge_provider", "github")
    assert isinstance(create_forge(github_client=gh_client), GitHubForge)


def test_provider_name_is_case_insensitive(gh_client):
    assert isinstance(
        create_forge("GitHub", github_client=gh_client), GitHubForge
    )


# ── Shared helpers ────────────────────────────────────────────


def test_timestamp_parsing_accepts_both_dialects():
    assert parse_timestamp("2026-01-02T03:04:05Z") is not None
    assert parse_timestamp("2026-01-02T03:04:05.000+00:00") is not None


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_timestamp_parsing_rejects_junk(value):
    assert parse_timestamp(value) is None


def test_both_adapters_satisfy_the_interface():
    from app.forge.base import Forge

    for adapter in (GitHubForge, GitLabForge):
        assert issubclass(adapter, Forge)
        assert not getattr(adapter, "__abstractmethods__", None), (
            f"{adapter.__name__} leaves interface methods unimplemented"
        )


def test_adapters_are_constructible_without_network(gh_client):
    assert GitHubForge(gh_client, 1).name == "github"
    assert GitLabForge("tok", client=Mock()).name == "gitlab"
