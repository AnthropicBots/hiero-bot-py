# app/github/client.py — Async GitHub API client

from __future__ import annotations
import time
import jwt
import httpx
from typing import Any, Optional
from app.utils.settings import settings
from app.utils.logger import get_logger

log = get_logger("github.client")

GITHUB_API = "https://api.github.com"


class GitHubClient:
    """Async GitHub App client. Generates installation tokens on demand."""

    def __init__(self) -> None:
        self._installation_tokens: dict[int, tuple[str, float]] = {}
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20.0,
        )

    #  Auth 
    def _make_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": settings.github_app_id}
        raw = settings.github_private_key
        # Handle all storage formats: literal \n, escaped \\n, or real newlines
        private_key = raw.replace("\\n", "\n").replace("\\\\n", "\n").strip()
        # If key has no newlines at all, it's likely stored as one long string
        if "\n" not in private_key and "BEGIN" in private_key:
            # Reconstruct proper PEM format
            header = "-----BEGIN RSA PRIVATE KEY-----"
            footer = "-----END RSA PRIVATE KEY-----"
            body = private_key.replace(header, "").replace(footer, "").strip()
            private_key = f"{header}\n{body}\n{footer}"
        log.debug("JWT private key starts with: %s", private_key[:40])
       return jwt.encode(payload, private_key, algorithm="RS256")

    async def _installation_token(self, installation_id: int) -> str:
        token, expires_at = self._installation_tokens.get(installation_id, ("", 0.0))
        if token and time.time() < expires_at - 60:
            return token

        resp = await self._http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._make_jwt()}"},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["token"]
        self._installation_tokens[installation_id] = (token, time.time() + 3600)
        return token

    def _app_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._make_jwt()}"}

    async def _inst_headers(self, installation_id: int) -> dict[str, str]:
        token = await self._installation_token(installation_id)
        return {"Authorization": f"token {token}"}

    #  Raw request 

    async def request(
        self,
        method: str,
        path: str,
        installation_id: int,
        **kwargs: Any,
    ) -> Any:
        headers = await self._inst_headers(installation_id)
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 404:
            raise httpx.HTTPStatusError("Not found", request=resp.request, response=resp)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}

    async def get(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("GET", path, installation_id, **kwargs)

    async def post(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("POST", path, installation_id, **kwargs)

    async def patch(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, installation_id, **kwargs)

    async def delete(self, path: str, installation_id: int, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, installation_id, **kwargs)

    #  High-level helpers 

    async def get_file_content(
        self, owner: str, repo: str, path: str, installation_id: int = 0
    ) -> Optional[str]:
        """Returns base64-encoded file content or None if not found."""
        try:
            if installation_id:
                data = await self.get(f"/repos/{owner}/{repo}/contents/{path}", installation_id)
            else:
                resp = await self._http.get(
                    f"/repos/{owner}/{repo}/contents/{path}",
                    headers=self._app_headers(),
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
        await self.post(f"/repos/{owner}/{repo}/issues/{number}/comments",
                        installation_id, json={"body": body})

    async def add_label(
        self, owner: str, repo: str, number: int, label: str, installation_id: int
    ) -> None:
        # Ensure label exists
        try:
            await self.get(f"/repos/{owner}/{repo}/labels/{label}", installation_id)
        except httpx.HTTPStatusError:
            await self.post(f"/repos/{owner}/{repo}/labels", installation_id,
                            json={"name": label, "color": "ededed"})
        await self.post(f"/repos/{owner}/{repo}/issues/{number}/labels",
                        installation_id, json={"labels": [label]})

    async def add_assignees(
        self, owner: str, repo: str, number: int,
        assignees: list[str], installation_id: int
    ) -> None:
        await self.post(f"/repos/{owner}/{repo}/issues/{number}/assignees",
                        installation_id, json={"assignees": assignees})

    async def remove_assignees(
        self, owner: str, repo: str, number: int,
        assignees: list[str], installation_id: int
    ) -> None:
        await self.delete(f"/repos/{owner}/{repo}/issues/{number}/assignees",
                          installation_id, json={"assignees": assignees})

    async def close_issue(
        self, owner: str, repo: str, number: int, installation_id: int
    ) -> None:
        await self.patch(f"/repos/{owner}/{repo}/issues/{number}",
                         installation_id,
                         json={"state": "closed", "state_reason": "not_planned"})

    async def list_issues(
        self, owner: str, repo: str, installation_id: int, **params: Any
    ) -> list[dict]:
        return await self.get(f"/repos/{owner}/{repo}/issues",
                              installation_id, params={"per_page": 100, **params})

    async def list_pr_files(
        self, owner: str, repo: str, pr_number: int, installation_id: int
    ) -> list[dict]:
        return await self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                              installation_id, params={"per_page": 100})

    async def list_pr_commits(
        self, owner: str, repo: str, pr_number: int, installation_id: int
    ) -> list[dict]:
        return await self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}/commits",
                              installation_id, params={"per_page": 100})

    async def list_pr_reviews(
        self, owner: str, repo: str, pr_number: int, installation_id: int
    ) -> list[dict]:
        return await self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                              installation_id, params={"per_page": 100})

    async def get_combined_status(
        self, owner: str, repo: str, sha: str, installation_id: int
    ) -> dict:
        return await self.get(f"/repos/{owner}/{repo}/commits/{sha}/status",
                              installation_id)

    async def get_user(self, login: str, installation_id: int) -> dict:
        return await self.get(f"/users/{login}", installation_id)

    async def list_team_members(
        self, org: str, team_slug: str, installation_id: int
    ) -> list[dict]:
        try:
            return await self.get(f"/orgs/{org}/teams/{team_slug}/members",
                                  installation_id, params={"per_page": 100})
        except Exception:
            return []

    async def list_installations(self) -> list[dict]:
        resp = await self._http.get("/app/installations",
                                    headers=self._app_headers(),
                                    params={"per_page": 100})
        resp.raise_for_status()
        return resp.json()

    async def list_installation_repos(self, installation_id: int) -> list[dict]:
        data = await self.get("/installation/repositories",
                              installation_id, params={"per_page": 100})
        return data.get("repositories", [])

    async def create_pr_review_comment(
        self, owner: str, repo: str, pr_number: int,
        body: str, path: str, line: int, commit_sha: str,
        installation_id: int,
    ) -> None:
        try:
            await self.post(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                installation_id,
                json={"body": body, "path": path, "line": line,
                      "side": "RIGHT", "commit_id": commit_sha},
            )
        except Exception as exc:
            log.warning("Inline comment failed (path=%s line=%d): %s", path, line, exc)

    async def close(self) -> None:
        await self._http.aclose()
