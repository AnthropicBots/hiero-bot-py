# app/forge/factory.py — Provider selection

from __future__ import annotations

from app.forge.base import Forge, ForgeError
from app.forge.github import GitHubForge
from app.forge.gitlab import GitLabForge
from app.github.client import GitHubClient
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("forge.factory")

SUPPORTED_PROVIDERS = ("github", "gitlab")


def create_forge(
    provider: str | None = None,
    *,
    github_client: GitHubClient | None = None,
    installation_id: int = 0,
) -> Forge:
    """
    Build the adapter for `provider`, defaulting to the configured one.

    GitHub needs the installation-aware client the app already owns; GitLab
    needs a personal or group access token. Anything else is a configuration
    error, raised here rather than surfacing later as a mysterious attribute
    error on a None.
    """
    provider = (provider or settings.forge_provider or "github").lower()

    if provider == "github":
        if github_client is None:
            raise ForgeError("GitHub forge requires a GitHubClient instance")
        return GitHubForge(github_client, installation_id)

    if provider == "gitlab":
        if not settings.gitlab_token:
            raise ForgeError("GITLAB_TOKEN must be set to use the GitLab forge")
        return GitLabForge(
            settings.gitlab_token,
            base_url=settings.gitlab_base_url,
        )

    raise ForgeError(
        f"Unknown forge provider {provider!r}. "
        f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
    )
