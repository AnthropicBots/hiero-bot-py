# app/forge/base.py — The contract every forge adapter implements

from __future__ import annotations

from abc import ABC, abstractmethod

from app.forge.models import (
    ForgeComment,
    ForgeFile,
    ForgeIssue,
    ForgePullRequest,
    ForgeUser,
)


class ForgeError(Exception):
    """A forge rejected or failed a request."""


class ForgeNotFound(ForgeError):
    """The requested resource does not exist, or the token cannot see it."""


class ForgeAuthError(ForgeError):
    """Credentials are missing, expired, or insufficient for the operation."""


class Forge(ABC):
    """
    The operations the workflows need from a code-hosting platform.

    Deliberately small. Everything here has a direct equivalent on GitHub,
    GitLab, Gitea and Forgejo; anything that only one platform offers stays on
    that platform's adapter rather than widening this interface and forcing the
    others to raise NotImplementedError.

    Every method takes `owner` and `repo` rather than a pre-bound project,
    because a single installation serves many repositories and binding at
    construction would mean one adapter instance per repo.
    """

    #: Short identifier used in config and settings, e.g. "github".
    name: str = ""

    # ── Reading ───────────────────────────────────────────────

    @abstractmethod
    async def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """Decoded file contents, or None when the path does not exist."""

    @abstractmethod
    async def list_issues(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[ForgeIssue]:
        """Every issue matching `state`, following pagination to the end."""

    @abstractmethod
    async def get_issue(self, owner: str, repo: str, number: int) -> ForgeIssue:
        ...

    @abstractmethod
    async def get_pull_request(
        self, owner: str, repo: str, number: int
    ) -> ForgePullRequest:
        ...

    @abstractmethod
    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[ForgeFile]:
        ...

    @abstractmethod
    async def list_comments(
        self, owner: str, repo: str, number: int
    ) -> list[ForgeComment]:
        ...

    @abstractmethod
    async def get_user(self, login: str) -> ForgeUser:
        ...

    # ── Writing ───────────────────────────────────────────────

    @abstractmethod
    async def post_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> None:
        ...

    @abstractmethod
    async def update_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> None:
        ...

    @abstractmethod
    async def add_label(self, owner: str, repo: str, number: int, label: str) -> None:
        ...

    @abstractmethod
    async def add_assignees(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        ...

    @abstractmethod
    async def remove_assignees(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        ...

    @abstractmethod
    async def close_issue(self, owner: str, repo: str, number: int) -> None:
        ...

    @abstractmethod
    async def request_reviewers(
        self, owner: str, repo: str, number: int, logins: list[str]
    ) -> None:
        ...

    # ── Lifecycle ─────────────────────────────────────────────

    async def close(self) -> None:
        """Release any held connections. Adapters override when they own a client."""
        return
