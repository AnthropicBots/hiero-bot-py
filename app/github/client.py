# app/github/client.py — Async GitHub API client
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
import jwt

from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("github.client")

GITHUB_API = "https://api.github.com"

# Safety valve for paginated endpoints: 100 items/page × 50 pages = 5000 items.
# Large enough for any realistic repository, small enough that a runaway loop
# can't burn an installation's whole rate-limit budget.
MAX_PAGES = 50

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
    - Escaped \n characters
    - Flattened single-line PEM
    - RSA, PKCS#8 and EC private keys
    """

    key = raw.replace("\\n", "\n").strip()

    # Already a valid multi-line PEM
    if "\n" in key and "BEGIN" in key and "END" in key:
        return key

    if "BEGIN" not in key or "END" not in key:
        raise RuntimeError(
            "Invalid GITHUB_PRIVATE_KEY. "
            "Expected a PEM formatted private key."
        )

    for header, footer in _PEM_MARKERS:
        if header in key and footer in key:
            body = (
                key.replace(header, "")
                .replace(footer, "")
                .strip()
            )

            return f"{header}\n{body}\n{footer}"

    raise RuntimeError(
        "Unsupported GITHUB_PRIVATE_KEY format. "
        "Supported PEM types are:\n"
        "- RSA PRIVATE KEY\n"
        "- PRIVATE KEY\n"
        "- EC PRIVATE KEY"
    )


class GitHubClient:
    """Async GitHub App client. Generates installation tokens on demand."""

    def __init__(self) -> None:
        self._installation_tokens: dict[int, tuple[str, float]] = {}
        # Per-installation asyncio locks to prevent thundering herd / race conditions
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20.0,
        )

    # Auth

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

    async def _installation_token(self, installation_id: int) -> str:
        # Initial cache check outside lock
        token, expires_at = self._installation_tokens.get(installation_id, ("", 0.0))
        if token and time.time() < expires_at - 60:
            return token

        # Fetch or create lock for this specific installation
        lock = self._refresh_locks.setdefault(
            installation_id,
            asyncio.Lock(),
        )

        async with lock:
            # Double-check cache inside lock in case another request refreshed it while waiting
            token, expires_at = self._installation_tokens.get(installation_id, ("", 0.0))
            if token and time.time() < expires_at - 60:
                return token

            # Execute refresh request only if token is still expired
            resp = await self._http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._make_jwt()}"},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data["token"]

            # Parse exact expires_at from GitHub API response
            expires_at_str = data.get("expires_at")
            try:
                if expires_at_str:
                    expiry = datetime.fromisoformat(
                        expires_at_str.replace("Z", "+00:00")
                    ).timestamp()
                else:
                    expiry = time.time() + 3600
            except (ValueError, TypeError):
                expiry = time.time() + 3600

            self._installation_tokens[installation_id] = (token, expiry)
            return token

    def _app_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._make_jwt()}"}

    async def _inst_headers(self, installation_id: int) -> dict[str, str]:
        token = await self._installation_token(installation_id)
        return {"Authorization": f"Bearer {token}"}

    # Raw request

    async def request(
        self,
        method: str,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        headers = await self._inst_headers(installation_id)
        max_retries = 3
        backoff = 1.0

        for attempt in range(max_retries + 1):
            try:
                resp = await self._http.request(method, path, headers=headers, **kwargs)
                if resp.status_code == 404:
                    raise httpx.HTTPStatusError(
                        "Not found", request=resp.request, response=resp
                    )

                # Retry on rate limiting (429) or transient gateway errors (502, 503, 504)
                if resp.status_code in (429, 502, 503, 504) and attempt < max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after else backoff
                    log.warning(
                        "GitHub API %s %s returned status %d. Retrying in %.1fs (attempt %d/%d)",
                        method, path, resp.status_code, sleep_time, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(sleep_time)
                    backoff *= 2.0
                    continue

                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}

            except httpx.RequestError as exc:
                if attempt < max_retries:
                    log.warning(
                        "GitHub API request network error on %s %s: %s. Retrying in %.1fs...",
                        method, path, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise

    async def get(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("GET", path, installation_id, **kwargs)

    async def post(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("POST", path, installation_id, **kwargs)

    async def patch(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, installation_id, **kwargs)

    async def delete(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, installation_id, **kwargs)

    # Pagination

    async def paginate(
        self,
        path: str,
        installation_id: int,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
        max_pages: int = MAX_PAGES,
        extract: str | None = None,
    ) -> list[dict]:
        """
        Walk a paginated GitHub collection endpoint and return every item.

        GitHub caps `per_page` at 100, so any endpoint queried without following
        pages silently truncates once a repository grows past that. Callers get
        the full collection here, bounded by ``max_pages`` so a pathological repo
        can't turn one scan into thousands of requests.

        ``extract`` names the key holding the list for endpoints that wrap their
        results in an object (e.g. ``/installation/repositories`` →
        ``repositories``).
        """
        items: list[dict] = []
        page = 1

        while page <= max_pages:
            result = await self.get(
                path,
                installation_id,
                params={**(params or {}), "per_page": per_page, "page": page},
            )

            batch = result.get(extract, []) if extract else result
            if not isinstance(batch, list):
                log.warning(
                    "Unexpected paginated payload for %s (page %d): %s",
                    path,
                    page,
                    type(batch).__name__,
                )
                break

            items.extend(batch)

            # A short page means we've reached the end of the collection.
            if len(batch) < per_page:
                return items

            page += 1

        log.warning(
            "Pagination for %s stopped at the %d-page cap — results are truncated",
            path,
            max_pages,
        )
        return items

    async def _paginate_app(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Same as :meth:`paginate` but authenticated as the app, not an installation."""
        items: list[dict] = []
        page = 1

        while page <= max_pages:
            resp = await self._http.get(
                path,
                headers=self._app_headers(),
                params={**(params or {}), "per_page": per_page, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()

            if not isinstance(batch, list):
                break

            items.extend(batch)

            if len(batch) < per_page:
                return items

            page += 1

        log.warning(
            "App-level pagination for %s stopped at the %d-page cap", path, max_pages
        )
        return items

    # High-level helpers

    async def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        installation_id: int = 0,
        ref: str | None = None,
    ) -> str | None:
        """Returns base64-encoded file content or None if not found."""
        try:
            params = {"ref": ref} if ref else None
            if installation_id:
                data = await self.get(
                    f"/repos/{owner}/{repo}/contents/{path}",
                    installation_id,
                    params=params,
                )
            else:
                resp = await self._http.get(
                    f"/repos/{owner}/{repo}/contents/{path}",
                    headers=self._app_headers(),
                    params=params,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
            return data.get("content")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def post_comment(
        self, owner: str, repo: str, number: int, body: str, installation_id: int
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
    ) -> list[dict]:
        comments: list[dict] = []
        page = 1

        while True:
            result = await self.get(
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                installation_id,
                params={"per_page": 100, "page": page},
            )

            if not isinstance(result, list):
                break

            comments.extend(result)

            if len(result) < 100:
                break

            page += 1

        return comments

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
        self, owner: str, repo: str, number: int, label: str, installation_id: int
    ) -> None:
        # Ensure label exists
        try:
            await self.get(f"/repos/{owner}/{repo}/labels/{label}", installation_id)
        except httpx.HTTPStatusError:
            await self.post(
                f"/repos/{owner}/{repo}/labels",
                installation_id,
                json={"name": label, "color": "ededed"},
            )
        await self.post(
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            installation_id,
            json={"labels": [label]},
        )

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
        """Request reviews from one or more reviewers on a pull request."""
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
        self, owner: str, repo: str, number: int, installation_id: int
    ) -> None:
        await self.patch(
            f"/repos/{owner}/{repo}/issues/{number}",
            installation_id,
            json={"state": "closed", "state_reason": "not_planned"},
        )

    async def list_issues(
        self, owner: str, repo: str, installation_id: int, **params: Any
    ) -> list[dict]:
        """List issues across every page — repos with >100 open issues need this."""
        return await self.paginate(
            f"/repos/{owner}/{repo}/issues",
            installation_id,
            params=params,
        )

    async def list_pr_files(
        self, owner: str, repo: str, pr_number: int, installation_id: int
    ) -> list[dict]:
        return await self.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            installation_id,
            params={"per_page": 100},
        )

    async def list_pr_commits(
        self, owner: str, repo: str, pr_number: int, installation_id: int
    ) -> list[dict]:
        return await self.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            installation_id,
            params={"per_page": 100},
        )

    async def list_pr_reviews(
        self, owner: str, repo: str, pr_number: int, installation_id: int
    ) -> list[dict]:
        return await self.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            installation_id,
            params={"per_page": 100},
        )

    async def get_combined_status(
        self, owner: str, repo: str, sha: str, installation_id: int
    ) -> dict:
        return await self.get(
            f"/repos/{owner}/{repo}/commits/{sha}/status", installation_id
        )

    async def get_user(self, login: str, installation_id: int) -> dict:
        return await self.get(f"/users/{login}", installation_id)

    async def search_issues(
        self,
        query: str,
        installation_id: int,
        *,
        per_page: int = 1,
        sort: str | None = None,
        order: str | None = None,
    ) -> dict:
        """
        Run an issue/PR search and return the raw response.

        The useful part is `total_count`: it answers "how many PRs did this
        person get merged?" in a single request, where walking `/pulls` would
        take one request per hundred PRs in the repository's entire history.
        Search has its own, much tighter rate limit, so callers should treat a
        failure here as "fall back to REST" rather than as fatal.
        """
        params: dict[str, Any] = {"q": query, "per_page": per_page}
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order

        return await self.get("/search/issues", installation_id, params=params)

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
        return data.get("permission", "none")

    async def list_team_members(
        self, org: str, team_slug: str, installation_id: int
    ) -> list[dict]:
        try:
            return await self.get(
                f"/orgs/{org}/teams/{team_slug}/members",
                installation_id,
                params={"per_page": 100},
            )
        except Exception:
            return []

    async def list_installations(self) -> list[dict]:
        """Every installation of this app, across all pages."""
        return await self._paginate_app("/app/installations")

    async def list_installation_repos(self, installation_id: int) -> list[dict]:
        """Every repo an installation can see — the response wraps them in an object."""
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
        except Exception as exc:
            log.warning("Inline comment failed (path=%s line=%d): %s", path, line, exc)

    async def list_commits(
        self,
        owner: str,
        repo: str,
        installation_id: int,
        *,
        path: str | None = None,
        per_page: int = 30,
    ) -> list[dict]:
        """
        Recent commits on the default branch, optionally scoped to one path.

        Scoping by path is what makes reviewer recommendation cheap: GitHub does
        the history walk server-side and returns only the commits that touched
        that directory, so one request answers "who has worked here lately".
        """
        params: dict[str, Any] = {"per_page": per_page}
        if path:
            params["path"] = path

        result = await self.get(
            f"/repos/{owner}/{repo}/commits", installation_id, params=params
        )
        return result if isinstance(result, list) else []

    async def close(self) -> None:
        await self._http.aclose()