# app/config/loader.py — Per-repo YAML config loader with TTL cache

from __future__ import annotations
import base64
import time
from typing import Optional
import yaml
from pydantic import ValidationError
from app.config.schema import RepoConfig
from app.utils.logger import get_logger

log = get_logger("config.loader")

_CONFIG_PATH = ".github/hiero-bot.yml"
_CACHE_TTL = 300  # 5 minutes


class ConfigLoader:
    def __init__(self, github_client: "GitHubClient") -> None:  # type: ignore[name-defined]
        self._client = github_client
        self._cache: dict[str, tuple[RepoConfig, float]] = {}

    async def load(self, owner: str, repo: str, installation_id: int = 0) -> Optional[RepoConfig]:
        """Load config, using cache if fresh. Returns None if no config file."""
        key = f"{owner}/{repo}"
        if key in self._cache:
            config, ts = self._cache[key]
            if time.monotonic() - ts < _CACHE_TTL:
                return config

        try:
            raw_b64 = await self._client.get_file_content(owner, repo, _CONFIG_PATH, installation_id)
            if raw_b64 is None:
                return None

            content = base64.b64decode(raw_b64).decode()
            data = yaml.safe_load(content)
            config = RepoConfig.model_validate(data)
            self._cache[key] = (config, time.monotonic())
            log.info("Loaded config for %s", key)
            return config

        except ValidationError as exc:
            log.error("Invalid config for %s: %s", key, exc)
            raise
        except Exception as exc:
            # 404 = not found = bot not enabled
            if getattr(exc, "status_code", None) == 404:
                log.debug("No config for %s — bot disabled", key)
                return None
            log.error("Failed loading config for %s: %s", key, exc)
            raise

    def invalidate(self, owner: str, repo: str) -> None:
        self._cache.pop(f"{owner}/{repo}", None)

    def clear(self) -> None:
        self._cache.clear()
