# app/forge/models.py — Forge-neutral representations

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ForgeUser:
    login: str
    id: int | None = None
    name: str | None = None
    is_bot: bool = False
    created_at: datetime | None = None
    public_repos: int = 0


@dataclass(frozen=True)
class ForgeIssue:
    """
    An issue as the workflows care about it.

    `number` is whatever the forge shows in its UI — GitHub's issue number,
    GitLab's `iid`. It is deliberately not the forge's internal database id,
    because every URL and every user-facing reference uses the display number.
    """

    number: int
    title: str
    state: str
    author: str
    url: str
    updated_at: datetime | None = None
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    is_pull_request: bool = False
    body: str = ""


@dataclass(frozen=True)
class ForgePullRequest:
    number: int
    title: str
    state: str
    author: str
    url: str
    source_branch: str = ""
    target_branch: str = ""
    head_sha: str = ""
    merged: bool = False
    merged_at: datetime | None = None
    draft: bool = False
    body: str = ""
    changed_files: int = 0


@dataclass(frozen=True)
class ForgeFile:
    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    patch: str = ""


@dataclass(frozen=True)
class ForgeComment:
    id: int
    body: str
    author: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class ForgeRepo:
    owner: str
    name: str
    default_branch: str = "main"
    private: bool = False
    topics: tuple[str, ...] = field(default=())

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse the ISO-8601 timestamps both GitHub and GitLab emit."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
