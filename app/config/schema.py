# app/github/client.py — Async GitHub API client
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("github.client")

GITHUB_API = "https://api.github.com"

# Safety valve for paginated endpoints:
# 100 items/page × 50 pages = 5000 items.
MAX_PAGES = 50

DEFAULT_PER_PAGE = 100
MAX_PER_PAGE = 100
MAX_RETRIES = 3
DEFAULT_BACKOFF = 1.0
REQUEST_TIMEOUT = 20.0

_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}

_PEM_MARKERS = [
    (
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----END RSA PRIVATE KEY-----",
    ),
    (
        "-----BEGIN PRIVATE KEY-----",
        "-----END PRIVATE KEY-----",
    ),
    (
        "-----BEGIN EC PRIVATE KEY-----",
        "-----END EC PRIVATE KEY-----",
    ),
]


def _normalize_private_key(raw: str) -> str:
    """
    Normalize GITHUB_PRIVATE_KEY into a valid multi-line PEM.

    Supports:
    - Already formatted PEM
    - Escaped \\n characters
    - Flattened single-line PEM
    - RSA, PKCS#8 and EC private keys
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(
            "Invalid GITHUB_PRIVATE_KEY. "
            "Expected a PEM formatted private key."
        )

    key = raw.replace("\\n", "\n").strip()

    for header, footer in _PEM_MARKERS:
        if header not in key or footer not in key:
            continue

        body = (
            key.replace(header, "")
            .replace(footer, "")
            .strip()
        )

        if not body:
            raise RuntimeError(
                "Invalid GITHUB_PRIVATE_KEY. "
                "The PEM private key body is empty."
            )

        # Normalize flattened PEM bodies into conventional 64-character
        # lines. Whitespace is removed first so accidental spaces/newlines
        # in environment variables do not affect the encoded key.
        body = "".join(body.split())
        body_lines = [
            body[index:index + 64]
            for index in range(0, len(body), 64)
        ]

        return f"{header}\n" + "\n".join(body_lines) + f"\n{footer}"

    raise RuntimeError(
        "Unsupported GITHUB_PRIVATE_KEY format. "
        "Supported PEM types are:\n"
        "- RSA PRIVATE KEY\n"
        "- PRIVATE KEY\n"
        "- EC PRIVATE KEY"
    )


def _validate_pagination(
    per_page: int,
    max_pages: int,
) -> None:
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise ValueError(
            f"per_page must be between 1 and {MAX_PER_PAGE}"
        )

    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(
            f"max_pages must be between 1 and {MAX_PAGES}"
        )


def _retry_delay(
    response: httpx.Response,
    backoff: float,
) -> float:
    """
    Return a safe Retry-After delay.

    Invalid or negative Retry-After values are ignored rather than allowing
    malformed server headers to break the retry loop.
    """
    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            delay = float(retry_after)
            if delay >= 0:
                return delay
        except (TypeError, ValueError):
            pass

    return backoff


class GitHubClient:
    """Async GitHub App client. Generates installation tokens on demand."""

    def __init__(self) -> None:
        self._installation_tokens: dict[int, tuple[str, float]] = {}

        # Per-installation locks prevent multiple concurrent requests from
        # refreshing the same installation token simultaneously.
        self._refresh_locks: dict[int, asyncio.Lock] = {}

        self._http = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=REQUEST_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _make_jwt(self) -> str:
        now = int(time.time())

        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": settings.github_app_id,
        }

        private_key = _normalize_private_key(
            settings.github_private_key
        )

        return jwt.encode(
            payload,
            private_key,
            algorithm="RS256",
        )

    async def _installation_token(
        self,
        installation_id: int,
    ) -> str:
        if installation_id <= 0:
            raise ValueError("installation_id must be a positive integer")

        # Fast cache check outside the lock.
        token, expires_at = self._installation_tokens.get(
            installation_id,
            ("", 0.0),
        )

        if token and time.time() < expires_at - 60:
            return token

        lock = self._refresh_locks.setdefault(
            installation_id,
            asyncio.Lock(),
        )

        async with lock:
            # Double-check after acquiring the lock.
            token, expires_at = self._installation_tokens.get(
                installation_id,
                ("", 0.0),
            )

            if token and time.time() < expires_at - 60:
                return token

            response = await self._http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._make_jwt()}",
                },
            )

            response.raise_for_status()

            data = response.json()

            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise RuntimeError(
                    "GitHub installation token response did not contain "
                    "a valid token."
                )

            expires_at_str = data.get("expires_at")

            try:
                if expires_at_str:
                    expiry = datetime.fromisoformat(
                        expires_at_str.replace("Z", "+00:00")
                    ).timestamp()
                else:
                    expiry = time.time() + 3600
            except (ValueError, TypeError):
                log.warning(
                    "Invalid expires_at returned for installation %d; "
                    "using one-hour fallback",
                    installation_id,
                )
                expiry = time.time() + 3600

            self._installation_tokens[installation_id] = (
                token,
                expiry,
            )

            return token

    def _app_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._make_jwt()}",
        }

    async def _inst_headers(
        self,
        installation_id: int,
    ) -> dict[str, str]:
        token = await self._installation_token(installation_id)

        return {
            "Authorization": f"Bearer {token}",
        }

    # ------------------------------------------------------------------
    # Raw request
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        headers = await self._inst_headers(installation_id)

        backoff = DEFAULT_BACKOFF

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._http.request(
                    method,
                    path,
                    headers=headers,
                    **kwargs,
                )

                if response.status_code == 404:
                    raise httpx.HTTPStatusError(
                        "Not found",
                        request=response.request,
                        response=response,
                    )

                if (
                    response.status_code in _RETRYABLE_STATUS_CODES
                    and attempt < MAX_RETRIES
                ):
                    sleep_time = _retry_delay(
                        response,
                        backoff,
                    )

                    log.warning(
                        "GitHub API %s %s returned status %d. "
                        "Retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        response.status_code,
                        sleep_time,
                        attempt + 1,
                        MAX_RETRIES,
                    )

                    await asyncio.sleep(sleep_time)
                    backoff *= 2.0
                    continue

                response.raise_for_status()

                if not response.content:
                    return {}

                return response.json()

            except httpx.RequestError as exc:
                if attempt >= MAX_RETRIES:
                    raise

                log.warning(
                    "GitHub API request network error on %s %s: %s. "
                    "Retrying in %.1fs (attempt %d/%d)",
                    method,
                    path,
                    exc,
                    backoff,
                    attempt + 1,
                    MAX_RETRIES,
                )

                await asyncio.sleep(backoff)
                backoff *= 2.0

        raise RuntimeError(
            f"Unable to complete GitHub API request: {method} {path}"
        )

    async def get(
        self,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        return await self.request(
            "GET",
            path,
            installation_id,
            **kwargs,
        )

    async def post(
        self,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        return await self.request(
            "POST",
            path,
            installation_id,
            **kwargs,
        )

    async def patch(
        self,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        return await self.request(
            "PATCH",
            path,
            installation_id,
            **kwargs,
        )

    async def delete(
        self,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        return await self.request(
            "DELETE",
            path,
            installation_id,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def paginate(
        self,
        path: str,
        installation_id: int,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: int = MAX_PAGES,
        extract: str | None = None,
    ) -> list[dict]:
        """
        Walk a paginated GitHub collection endpoint and return every item
        up to the configured page cap.

        If the page cap is reached, a warning is emitted because the
        returned collection may be truncated.

        ``extract`` names the key holding the list for endpoints that
        wrap their results in an object.
        """
        _validate_pagination(per_page, max_pages)

        items: list[dict] = []

        for page in range(1, max_pages + 1):
            result = await self.get(
                path,
                installation_id,
                params={
                    **(params or {}),
                    "per_page": per_page,
                    "page": page,
                },
            )

            batch = (
                result.get(extract, [])
                if extract
                else result
            )

            if not isinstance(batch, list):
                log.warning(
                    "Unexpected paginated payload for %s "
                    "(page %d): %s",
                    path,
                    page,
                    type(batch).__name__,
                )
                return items

            items.extend(batch)

            if len(batch) < per_page:
                return items

        log.warning(
            "Pagination for %s reached the %d-page cap; "
            "results may be truncated",
            path,
            max_pages,
        )

        return items

    async def count_assigned_open_issues(
        self,
        owner: str,
        repo: str,
        login: str,
        installation_id: int,
        *,
        max_pages: int = MAX_PAGES,
    ) -> int:
        """Count open issues assigned to a user, excluding pull requests."""
        items = await self.paginate(
            f"/repos/{owner}/{repo}/issues",
            installation_id,
            params={
                "assignee": login,
                "state": "open",
            },
            per_page=DEFAULT_PER_PAGE,
            max_pages=max_pages,
        )

        return sum(
            1
            for item in items
            if "pull_request" not in item
        )

    async def _paginate_app(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Paginate an endpoint authenticated as the GitHub App."""
        _validate_pagination(per_page, max_pages)

        items: list[dict] = []

        for page in range(1, max_pages + 1):
            backoff = DEFAULT_BACKOFF
            batch: Any = None

            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await self._http.get(
                        path,
                        headers=self._app_headers(),
                        params={
                            **(params or {}),
                            "per_page": per_page,
                            "page": page,
                        },
                    )

                    if (
                        response.status_code
                        in _RETRYABLE_STATUS_CODES
                        and attempt < MAX_RETRIES
                    ):
                        sleep_time = _retry_delay(
                            response,
                            backoff,
                        )

                        log.warning(
                            "GitHub App API %s page %d returned "
                            "status %d. Retrying in %.1fs "
                            "(attempt %d/%d)",
                            path,
                            page,
                            response.status_code,
                            sleep_time,
                            attempt + 1,
                            MAX_RETRIES,
                        )

                        await asyncio.sleep(sleep_time)
                        backoff *= 2.0
                        continue

                    response.raise_for_status()
                    batch = response.json()
                    break

                except httpx.RequestError as exc:
                    if attempt >= MAX_RETRIES:
                        raise

                    log.warning(
                        "GitHub App API network error on %s page %d: "
                        "%s. Retrying in %.1fs "
                        "(attempt %d/%d)",
                        path,
                        page,
                        exc,
                        backoff,
                        attempt + 1,
                        MAX_RETRIES,
                    )

                    await asyncio.sleep(backoff)
                    backoff *= 2.0

            if batch is None:
                raise RuntimeError(
                    f"Unable to fetch paginated GitHub App endpoint: "
                    f"{path}"
                )

            if not isinstance(batch, list):
                log.warning(
                    "Unexpected app-level paginated payload for %s "
                    "(page %d): %s",
                    path,
                    page,
                    type(batch).__name__,
                )
                return items

            items.extend(batch)

            if len(batch) < per_page:
                return items

        log.warning(
            "App-level pagination for %s reached the %d-page cap; "
            "results may be truncated",
            path,
            max_pages,
        )

        return items

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    async def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        installation_id: int = 0,
        ref: str | None = None,
    ) -> str | None:
        """Return base64-encoded file content or None if not found."""
        encoded_path = quote(path, safe="/")

        try:
            params = {"ref": ref} if ref else None

            if installation_id:
                data = await self.get(
                    f"/repos/{owner}/{repo}/contents/{encoded_path}",
                    installation_id,
                    params=params,
                )
            else:
                response = await self._http.get(
                    f"/repos/{owner}/{repo}/contents/{encoded_path}",
                    headers=self._app_headers(),
                    params=params,
                )

                if response.status_code == 404:
                    return None

                response.raise_for_status()
                data = response.json()

            content = data.get("content")

            return content if isinstance(content, str) else None

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def post_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        installation_id: int,
    ) -> None:
        await self.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            installation_id,
            json={"body": body},
        )

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        installation_id: int,
        *,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        return await self.paginate(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            installation_id,
            max_pages=max_pages,
        )

    async def update_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        body: str,
        installation_id: int,
    ) -> None:
        await self.patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            installation_id,
            json={"body": body},
        )

    async def add_label(
        self,
        owner: str,
        repo: str,
        number: int,
        label: str,
        installation_id: int,
    ) -> None:
        encoded_label = quote(label, safe="")

        # Only create the label when GitHub explicitly says it does not exist.
        try:
            await self.get(
                f"/repos/{owner}/{repo}/labels/{encoded_label}",
                installation_id,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

            await self.post(
                f"/repos/{owner}/{repo}/labels",
                installation_id,
                json={
                    "name": label,
                    "color": "ededed",
                },
            )

        await self.post(
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            installation_id,
            json={"labels": [label]},
        )

    async def remove_label(
        self,
        owner: str,
        repo: str,
        number: int,
        label: str,
        installation_id: int,
    ) -> None:
        """Remove a label, tolerating only an already-missing label."""
        encoded_label = quote(label, safe="")

        try:
            await self.delete(
                f"/repos/{owner}/{repo}/issues/{number}/labels/"
                f"{encoded_label}",
                installation_id,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    async def add_assignees(
        self,
        owner: str,
        repo: str,
        number: int,
        assignees: list[str],
        installation_id: int,
    ) -> None:
        await self.post(
            f"/repos/{owner}/{repo}/issues/{number}/assignees",
            installation_id,
            json={"assignees": assignees},
        )

    async def request_reviewers(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reviewers: list[str],
        installation_id: int,
    ) -> None:
        """Request reviews from one or more reviewers."""
        if not reviewers:
            return

        await self.post(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
            installation_id,
            json={"reviewers": reviewers},
        )

    async def remove_assignees(
        self,
        owner: str,
        repo: str,
        number: int,
        assignees: list[str],
        installation_id: int,
    ) -> None:
        await self.delete(
            f"/repos/{owner}/{repo}/issues/{number}/assignees",
            installation_id,
            json={"assignees": assignees},
        )

    async def close_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        installation_id: int,
    ) -> None:
        await self.patch(
            f"/repos/{owner}/{repo}/issues/{number}",
            installation_id,
            json={
                "state": "closed",
                "state_reason": "not_planned",
            },
        )

    async def list_issues(
        self,
        owner: str,
        repo: str,
        installation_id: int,
        **params: Any,
    ) -> list[dict]:
        """List issues across every page."""
        return await self.paginate(
            f"/repos/{owner}/{repo}/issues",
            installation_id,
            params=params,
        )

    async def list_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        installation_id: int,
        *,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        return await self.paginate(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            installation_id,
            max_pages=max_pages,
        )

    async def list_pr_commits(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        installation_id: int,
        *,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        return await self.paginate(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            installation_id,
            max_pages=max_pages,
        )

    async def list_pr_reviews(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        installation_id: int,
        *,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        return await self.paginate(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            installation_id,
            max_pages=max_pages,
        )

    async def get_combined_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        installation_id: int,
    ) -> dict:
        return await self.get(
            f"/repos/{owner}/{repo}/commits/{sha}/status",
            installation_id,
        )

    async def get_user(
        self,
        login: str,
        installation_id: int,
    ) -> dict:
        return await self.get(
            f"/users/{login}",
            installation_id,
        )

    async def search_issues(
        self,
        query: str,
        installation_id: int,
        *,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
        sort: str | None = None,
        order: str | None = None,
    ) -> dict:
        """
        Run a GitHub issue/PR search and return one result page.
        """
        if not 1 <= per_page <= MAX_PER_PAGE:
            raise ValueError(
                f"per_page must be between 1 and {MAX_PER_PAGE}"
            )

        if page < 1:
            raise ValueError("page must be at least 1")

        params: dict[str, Any] = {
            "q": query,
            "per_page": per_page,
            "page": page,
        }

        if sort:
            params["sort"] = sort

        if order:
            params["order"] = order

        return await self.get(
            "/search/issues",
            installation_id,
            params=params,
        )

    async def paginate_search(
        self,
        query: str,
        installation_id: int,
        *,
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: int = MAX_PAGES,
        sort: str | None = None,
        order: str | None = None,
    ) -> list[dict]:
        """Return all matching search results up to the page cap."""
        _validate_pagination(per_page, max_pages)

        items: list[dict] = []

        for page in range(1, max_pages + 1):
            result = await self.search_issues(
                query,
                installation_id,
                per_page=per_page,
                page=page,
                sort=sort,
                order=order,
            )

            batch = result.get("items", [])

            if not isinstance(batch, list):
                log.warning(
                    "Unexpected search payload for %s "
                    "(page %d): %s",
                    query,
                    page,
                    type(batch).__name__,
                )
                return items

            items.extend(batch)

            if len(batch) < per_page:
                return items

        log.warning(
            "Search pagination for %s reached the %d-page cap; "
            "results may be truncated",
            query,
            max_pages,
        )

        return items

    async def get_collaborator_permission(
        self,
        owner: str,
        repo: str,
        login: str,
        installation_id: int,
    ) -> str:
        data = await self.get(
            f"/repos/{owner}/{repo}/collaborators/{login}/permission",
            installation_id,
        )

        permission = data.get("permission", "none")

        return (
            permission
            if isinstance(permission, str)
            else "none"
        )

    async def list_team_members(
        self,
        org: str,
        team_slug: str,
        installation_id: int,
    ) -> list[dict]:
        try:
            result = await self.paginate(
                f"/orgs/{org}/teams/{team_slug}/members",
                installation_id,
            )

            return result

        except httpx.HTTPStatusError as exc:
            # Preserve the original behavior of treating unavailable team
            # membership data as empty, but only for authorization/not-found
            # cases. Server failures should remain visible.
            if exc.response.status_code in (403, 404):
                return []
            raise

    async def list_installations(self) -> list[dict]:
        """Every installation of this app, across all pages."""
        return await self._paginate_app(
            "/app/installations"
        )

    async def list_installation_repos(
        self,
        installation_id: int,
    ) -> list[dict]:
        """Every repo an installation can see."""
        return await self.paginate(
            "/installation/repositories",
            installation_id,
            extract="repositories",
        )

    async def create_pr_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        path: str,
        line: int,
        commit_sha: str,
        installation_id: int,
    ) -> None:
        try:
            await self.post(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                installation_id,
                json={
                    "body": body,
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "commit_id": commit_sha,
                },
            )

        except httpx.HTTPStatusError as exc:
            # Inline comments can legitimately fail when a line is no longer
            # commentable. Other API failures should not disappear silently.
            if exc.response.status_code in (400, 404, 422):
                log.warning(
                    "Inline comment rejected "
                    "(path=%s line=%d status=%d): %s",
                    path,
                    line,
                    exc.response.status_code,
                    exc,
                )
                return

            raise

    async def list_commits(
        self,
        owner: str,
        repo: str,
        installation_id: int,
        *,
        path: str | None = None,
        per_page: int = 30,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """
        Recent commits on the default branch, optionally scoped to one path.

        Scoping by path lets GitHub perform the history filtering server-side.
        """
        _validate_pagination(per_page, max_pages)

        params: dict[str, Any] = {
            "per_page": per_page,
        }

        if path:
            params["path"] = path

        result = await self.paginate(
            f"/repos/{owner}/{repo}/commits",
            installation_id,
            params=params,
            per_page=per_page,
            max_pages=max_pages,
        )

        return result

    async def close(self) -> None:
        await self._http.aclose()

        self._installation_tokens.clear()
        self._refresh_locks.clear()
