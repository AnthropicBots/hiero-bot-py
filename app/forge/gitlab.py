# app/forge/gitlab.py — GitLab / Gitea-compatible adapter (GitLab REST v4)

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.forge.base import Forge, ForgeAuthError, ForgeError, ForgeNotFound
from app.forge.models import (
    ForgeComment,
    ForgeFile,
    ForgeIssue,
    ForgePullRequest,
    ForgeUser,
    parse_timestamp,
)
from app.utils.logger import get_logger

log = get_logger("forge.gitlab")

DEFAULT_BASE_URL = "https://gitlab.com/api/v4"
MAX_PAGES = 50


def project_id(owner: str, repo: str) -> str:
    """
    GitLab addresses projects by URL-encoded path.

    The slash must be encoded too, which is why `safe=""` matters: a raw slash
    would be read as a path separator and the request would 404.
    """
    return quote(f"{owner}/{repo}", safe="")


class GitLabForge(Forge):
    """
    GitLab adapter.

    Vocabulary differences that matter and are handled here:

    * Pull requests are *merge requests*, at `/merge_requests`.
    * Comments are *notes*.
    * Issues and merge requests are addressed by `iid` (per-project display
      number), not the global `id`. Using `id` silently targets a different
      project's issue.
    * Assignees and reviewers are set by numeric user id, not username, so
      logins are resolved through `/users?username=` and cached.
    * Labels are set by updating the issue, not by posting to a sub-resource.
    """

    name = "gitlab"

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ForgeAuthError("GitLab adapter requires an access token")

        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"PRIVATE-TOKEN": token},
            timeout=20.0,
        )
        self._user_ids: dict[str, int] = {}

    # ── HTTP plumbing ─────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ForgeError(f"GitLab request failed: {exc}") from exc

        if response.status_code == 404:
            raise ForgeNotFound(f"GitLab 404 for {path}")
        if response.status_code in (401, 403):
            raise ForgeAuthError(f"GitLab rejected credentials for {path}")
        if response.status_code >= 400:
            raise ForgeError(
                f"GitLab {response.status_code} for {path}: {response.text[:200]}"
            )

        if not response.content:
            return {}
        return response.json()

    async def _paginate(self, path: str, **params: Any) -> list[dict]:
        items: list[dict] = []
        page = 1

        while page <= MAX_PAGES:
            batch = await self._request(
                "GET", path, params={**params, "per_page": 100, "page": page}
            )
            if not isinstance(batch, list):
                break

            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

        log.warning("GitLab pagination for %s hit the %d-page cap", path, MAX_PAGES)
        return items

    async def _user_id(self, login: str) -> int | None:
        if login in self._user_ids:
            return self._user_ids[login]

        matches = await self._request("GET", "/users", params={"username": login})
        if not matches:
            log.warning("No GitLab user found for @%s", login)
            return None

        self._user_ids[login] = matches[0]["id"]
        return self._user_ids[login]

    async def _user_ids_for(self, logins: list[str]) -> list[int]:
        resolved = [await self._user_id(login) for login in logins]
        return [user_id for user_id in resolved if user_id is not None]

    # ── Reading ───────────────────────────────────────────────

    async def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        encoded = quote(path, safe="")
        try:
            raw = await self._request(
                "GET",
                f"/projects/{project_id(owner, repo)}/repository/files/{encoded}/raw",
                params={"ref": ref or "HEAD"},
            )
        except ForgeNotFound:
            return None

        return raw if isinstance(raw, str) else str(raw)

    async def list_issues(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[ForgeIssue]:
        raw = await self._paginate(
            f"/projects/{project_id(owner, repo)}/issues",
            state=_state_filter(state),
        )
        return [self._issue(item) for item in raw]

    async def get_issue(self, owner: str, repo: str, number: int) -> ForgeIssue:
        raw = await self._request(
            "GET", f"/projects/{project_id(owner, repo)}/issues/{number}"
        )
        return self._issue(raw)

    async def get_pull_request(
        self, owner: str, repo: str, number: int
    ) -> ForgePullRequest:
        raw = await self._request(
            "GET", f"/projects/{project_id(owner, repo)}/merge_requests/{number}"
        )
        return self._merge_request(raw)

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[ForgeFile]:
        raw = await self._request(
            "GET",
            f"/projects/{project_id(owner, repo)}/merge_requests/{number}/changes",
        )
        return [
            ForgeFile(
                path=change.get("new_path") or change.get("old_path", ""),
                status=_change_status(change),
                patch=change.get("diff", "") or "",
            )
            for change in (raw.get("changes") or [])
        ]

    async def list_comments(
        self, owner: str, repo: str, number: int
    ) -> list[ForgeComment]:
        raw = await self._paginate(
            f"/projects/{project_id(owner, repo)}/issues/{number}/notes"
        )
        return [
            ForgeComment(
                id=note.get("id", 0),
                body=note.get("body", ""),
                author=(note.get("author") or {}).get("username", ""),
                created_at=parse_timestamp(note.get("created_at")),
            )
            for note in raw
            if not note.get("system")
        ]

    async def get_user(self, login: str) -> ForgeUser:
        matches = await self._request("GET", "/users", params={"username": login})
        if not matches:
            raise ForgeNotFound(f"No GitLab user @{login}")

        raw = matches[0]
        return ForgeUser(
            login=raw.get("username", login),
            id=raw.get("id"),
            name=raw.get("name"),
            is_bot=raw.get("bot", False),
            created_at=parse_timestamp(raw.get("created_at")),
        )

    # ── Writing ───────────────────────────────────────────────

    async def post_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> None:
        await self._request(
            "POST",
            f"/projects/{project_id(owner, repo)}/issues/{number}/notes",
            json={"body": body},
        )

    async def update_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> None:
        # GitLab scopes note edits to their parent issue, so the caller-visible
        # signature keeps `comment_id` addressable only through the issue it
        # belongs to; we look it up by discussion-free note id on the project.
        await self._request(
            "PUT",
            f"/projects/{project_id(owner, repo)}/notes/{comment_id}",
            json={"body": body},
        )

    async def add_label(self, owner: str, repo: str, number: int, label: str) -> None:
        await self._request(
            "PUT",
            f"/projects/{project_id(owner, repo)}/issues/{number}",
            json={"add_labels": label},
        )

    async def add_assignees(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        ids = await self._user_ids_for(logins)
        if not ids:
            return

        current = await self.get_issue(owner, repo, number)
        existing = await self._user_ids_for(list(current.assignees))

        await self._request(
            "PUT",
            f"/projects/{project_id(owner, repo)}/issues/{number}",
            json={"assignee_ids": sorted(set(existing) | set(ids))},
        )

    async def remove_assignees(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        removing = set(await self._user_ids_for(logins))
        if not removing:
            return

        current = await self.get_issue(owner, repo, number)
        remaining = [
            user_id
            for user_id in await self._user_ids_for(list(current.assignees))
            if user_id not in removing
        ]

        await self._request(
            "PUT",
            f"/projects/{project_id(owner, repo)}/issues/{number}",
            json={"assignee_ids": remaining},
        )

    async def close_issue(self, owner: str, repo: str, number: int) -> None:
        await self._request(
            "PUT",
            f"/projects/{project_id(owner, repo)}/issues/{number}",
            json={"state_event": "close"},
        )

    async def request_reviewers(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        ids = await self._user_ids_for(logins)
        if not ids:
            return

        await self._request(
            "PUT",
            f"/projects/{project_id(owner, repo)}/merge_requests/{number}",
            json={"reviewer_ids": ids},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ── Translation ───────────────────────────────────────────

    @staticmethod
    def _issue(raw: dict) -> ForgeIssue:
        return ForgeIssue(
            number=raw.get("iid", 0),
            title=raw.get("title", ""),
            state=_normalize_state(raw.get("state", "opened")),
            author=(raw.get("author") or {}).get("username", ""),
            url=raw.get("web_url", ""),
            updated_at=parse_timestamp(raw.get("updated_at")),
            labels=tuple(raw.get("labels") or []),
            assignees=tuple(
                person.get("username", "")
                for person in (raw.get("assignees") or [])
                if person
            ),
            is_pull_request=False,
            body=raw.get("description") or "",
        )

    @staticmethod
    def _merge_request(raw: dict) -> ForgePullRequest:
        return ForgePullRequest(
            number=raw.get("iid", 0),
            title=raw.get("title", ""),
            state=_normalize_state(raw.get("state", "opened")),
            author=(raw.get("author") or {}).get("username", ""),
            url=raw.get("web_url", ""),
            source_branch=raw.get("source_branch", ""),
            target_branch=raw.get("target_branch", ""),
            head_sha=raw.get("sha", ""),
            merged=raw.get("state") == "merged",
            merged_at=parse_timestamp(raw.get("merged_at")),
            draft=bool(raw.get("draft") or raw.get("work_in_progress")),
            body=raw.get("description") or "",
            changed_files=raw.get("changes_count") or 0,
        )


def _normalize_state(state: str) -> str:
    """GitLab says opened/closed/merged; the neutral vocabulary is open/closed/merged."""
    return {"opened": "open"}.get(state, state)


def _state_filter(state: str) -> str:
    return {"open": "opened"}.get(state, state)


def _change_status(change: dict) -> str:
    if change.get("new_file"):
        return "added"
    if change.get("deleted_file"):
        return "removed"
    if change.get("renamed_file"):
        return "renamed"
    return "modified"
