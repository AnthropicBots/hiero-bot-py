# app/forge/github.py — GitHub adapter over the existing App client

from __future__ import annotations

import base64

import httpx

from app.forge.base import Forge, ForgeError, ForgeNotFound
from app.forge.models import (
    ForgeComment,
    ForgeFile,
    ForgeIssue,
    ForgePullRequest,
    ForgeUser,
    parse_timestamp,
)
from app.github.client import GitHubClient
from app.utils.logger import get_logger

log = get_logger("forge.github")


class GitHubForge(Forge):
    """
    Wraps `GitHubClient` without changing it.

    The client is already installation-aware and battle-tested; this adapter
    only translates its payloads into the neutral models and its exceptions
    into `ForgeError`. Existing callers of `GitHubClient` keep working
    untouched.
    """

    name = "github"

    def __init__(self, client: GitHubClient, installation_id: int) -> None:
        self._client = client
        self._installation_id = installation_id

    # ── Reading ───────────────────────────────────────────────

    async def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        raw_b64 = await self._client.get_file_content(
            owner, repo, path, self._installation_id, ref=ref
        )
        if raw_b64 is None:
            return None
        return base64.b64decode(raw_b64).decode("utf-8", errors="replace")

    async def list_issues(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[ForgeIssue]:
        with _translated():
            raw = await self._client.list_issues(
                owner, repo, self._installation_id, state=state
            )
        return [self._issue(item) for item in raw]

    async def get_issue(self, owner: str, repo: str, number: int) -> ForgeIssue:
        with _translated():
            raw = await self._client.get(
                f"/repos/{owner}/{repo}/issues/{number}", self._installation_id
            )
        return self._issue(raw)

    async def get_pull_request(
        self, owner: str, repo: str, number: int
    ) -> ForgePullRequest:
        with _translated():
            raw = await self._client.get(
                f"/repos/{owner}/{repo}/pulls/{number}", self._installation_id
            )
        return self._pull_request(raw)

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[ForgeFile]:
        with _translated():
            raw = await self._client.list_pr_files(
                owner, repo, number, self._installation_id
            )
        return [
            ForgeFile(
                path=item.get("filename", ""),
                status=item.get("status", "modified"),
                additions=item.get("additions", 0),
                deletions=item.get("deletions", 0),
                patch=item.get("patch", "") or "",
            )
            for item in raw
        ]

    async def list_comments(
        self, owner: str, repo: str, number: int
    ) -> list[ForgeComment]:
        with _translated():
            raw = await self._client.list_issue_comments(
                owner, repo, number, self._installation_id
            )
        return [
            ForgeComment(
                id=item.get("id", 0),
                body=item.get("body", ""),
                author=(item.get("user") or {}).get("login", ""),
                created_at=parse_timestamp(item.get("created_at")),
            )
            for item in raw
        ]

    async def get_user(self, login: str) -> ForgeUser:
        with _translated():
            raw = await self._client.get_user(login, self._installation_id)
        return ForgeUser(
            login=raw.get("login", login),
            id=raw.get("id"),
            name=raw.get("name"),
            is_bot=raw.get("type") == "Bot",
            created_at=parse_timestamp(raw.get("created_at")),
            public_repos=raw.get("public_repos") or 0,
        )

    # ── Writing ───────────────────────────────────────────────

    async def post_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> None:
        with _translated():
            await self._client.post_comment(
                owner, repo, number, body, self._installation_id
            )

    async def update_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> None:
        with _translated():
            await self._client.update_comment(
                owner, repo, comment_id, body, self._installation_id
            )

    async def add_label(self, owner: str, repo: str, number: int, label: str) -> None:
        with _translated():
            await self._client.add_label(
                owner, repo, number, label, self._installation_id
            )

    async def add_assignees(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        if not logins:
            return
        with _translated():
            await self._client.add_assignees(
                owner, repo, number, logins, self._installation_id
            )

    async def remove_assignees(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        if not logins:
            return
        with _translated():
            await self._client.remove_assignees(
                owner, repo, number, logins, self._installation_id
            )

    async def close_issue(self, owner: str, repo: str, number: int) -> None:
        with _translated():
            await self._client.close_issue(
                owner, repo, number, self._installation_id
            )

    async def request_reviewers(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        if not logins:
            return
        with _translated():
            await self._client.request_reviewers(
                owner, repo, number, logins, self._installation_id
            )

    # ── Translation ───────────────────────────────────────────

    @staticmethod
    def _issue(raw: dict) -> ForgeIssue:
        return ForgeIssue(
            number=raw.get("number", 0),
            title=raw.get("title", ""),
            state=raw.get("state", "open"),
            author=(raw.get("user") or {}).get("login", ""),
            url=raw.get("html_url", ""),
            updated_at=parse_timestamp(raw.get("updated_at")),
            labels=tuple(
                label["name"] if isinstance(label, dict) else str(label)
                for label in (raw.get("labels") or [])
            ),
            assignees=tuple(
                person["login"] for person in (raw.get("assignees") or []) if person
            ),
            is_pull_request="pull_request" in raw,
            body=raw.get("body") or "",
        )

    @staticmethod
    def _pull_request(raw: dict) -> ForgePullRequest:
        return ForgePullRequest(
            number=raw.get("number", 0),
            title=raw.get("title", ""),
            state=raw.get("state", "open"),
            author=(raw.get("user") or {}).get("login", ""),
            url=raw.get("html_url", ""),
            source_branch=(raw.get("head") or {}).get("ref", ""),
            target_branch=(raw.get("base") or {}).get("ref", ""),
            head_sha=(raw.get("head") or {}).get("sha", ""),
            merged=bool(raw.get("merged") or raw.get("merged_at")),
            merged_at=parse_timestamp(raw.get("merged_at")),
            draft=bool(raw.get("draft")),
            body=raw.get("body") or "",
            changed_files=raw.get("changed_files", 0),
        )


class _translated:
    """Context manager turning httpx failures into the forge-neutral hierarchy."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            if exc.response.status_code == 404:
                raise ForgeNotFound(str(exc)) from exc
            raise ForgeError(str(exc)) from exc
        if isinstance(exc, httpx.HTTPError):
            raise ForgeError(str(exc)) from exc
        return False
