# app/config/loader.py — Per-repo YAML config loader with TTL cache

from __future__ import annotations

import base64
import binascii
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import yaml
from pydantic import ValidationError

from app.config.schema import RepoConfig
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.github.client import GitHubClient

log = get_logger("config.loader")

_CONFIG_PATH = ".github/hiero-bot.yml"

# A repo with a config is the common case and its contents change rarely.
_CACHE_TTL = 300  # 5 minutes

# "No config" is cached too, but far more briefly: a maintainer who has just
# added the file should not have to wait five minutes for the bot to notice.
# Without this every webhook from every uninstrumented repo hit the contents
# API, which is the majority of traffic for an app installed org-wide.
_NEGATIVE_CACHE_TTL = 60

# Bound the cache so an org-wide installation can't grow it without limit.
_MAX_CACHE_ENTRIES = 512

# A bot config is a few kilobytes. Anything past this is a mistake or an
# attempt to make the parser do expensive work.
_MAX_CONFIG_BYTES = 128 * 1024


class ConfigError(Exception):
    """Base class for configuration problems attributable to a repository."""


class ConfigInvalid(ConfigError):
    """The config file exists but could not be parsed or validated."""

    def __init__(self, slug: str, detail: str) -> None:
        super().__init__(f"Invalid hiero-bot config for {slug}: {detail}")
        self.slug = slug
        self.detail = detail


@dataclass
class _CacheEntry:
    config: RepoConfig | None
    expires_at: float

    @property
    def fresh(self) -> bool:
        return time.monotonic() < self.expires_at


class ConfigLoader:
    def __init__(self, github_client: GitHubClient) -> None:
        self._client = github_client
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    async def load(
        self, owner: str, repo: str, installation_id: int = 0
    ) -> RepoConfig | None:
        """
        Load a repo's config, using the cache if fresh.

        Returns None when the repository has no config file — the bot stays
        completely silent in that case. Raises ConfigInvalid when a file exists
        but is unusable, so the problem surfaces instead of the repo silently
        behaving as if the bot were uninstalled.
        """
        key = f"{owner}/{repo}"

        entry = self._cache.get(key)
        if entry is not None and entry.fresh:
            self._hits += 1
            self._cache.move_to_end(key)
            return entry.config

        self._misses += 1

        try:
            raw_b64 = await self._client.get_file_content(
                owner, repo, _CONFIG_PATH, installation_id
            )
        except httpx.HTTPStatusError as exc:
            # `get_file_content` already maps 404 to None, but a 404 can still
            # arrive here from another layer. The previous version tested
            # `getattr(exc, "status_code")`, which httpx.HTTPStatusError does
            # not define — the branch could never fire, so a missing config
            # propagated as a 500 instead of disabling the bot for that repo.
            if exc.response.status_code == 404:
                log.debug("No config for %s — bot disabled", key)
                self._store(key, None)
                return None
            log.error("Failed loading config for %s: %s", key, exc)
            raise

        if raw_b64 is None:
            log.debug("No config for %s — bot disabled", key)
            self._store(key, None)
            return None

        config = self._parse(key, raw_b64)
        self._store(key, config)
        log.info("Loaded config for %s", key)
        return config

    # ── Parsing ───────────────────────────────────────────────

    @staticmethod
    def _parse(slug: str, raw_b64: str) -> RepoConfig:
        try:
            raw = base64.b64decode(raw_b64)
        except (binascii.Error, ValueError) as exc:
            raise ConfigInvalid(slug, f"content is not valid base64 ({exc})") from exc

        if len(raw) > _MAX_CONFIG_BYTES:
            raise ConfigInvalid(
                slug,
                f"file is {len(raw)} bytes, over the {_MAX_CONFIG_BYTES}-byte limit",
            )

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigInvalid(slug, "file is not valid UTF-8") from exc

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigInvalid(slug, f"YAML syntax error ({exc})") from exc

        if data is None:
            raise ConfigInvalid(slug, "file is empty")
        if not isinstance(data, dict):
            raise ConfigInvalid(
                slug, f"top level must be a mapping, got {type(data).__name__}"
            )

        try:
            return RepoConfig.model_validate(data)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:5]
            )
            log.error("Invalid config for %s: %s", slug, detail)
            raise ConfigInvalid(slug, detail) from exc

    # ── Cache management ──────────────────────────────────────

    def _store(self, key: str, config: RepoConfig | None) -> None:
        ttl = _CACHE_TTL if config is not None else _NEGATIVE_CACHE_TTL
        self._cache[key] = _CacheEntry(config, time.monotonic() + ttl)
        self._cache.move_to_end(key)

        while len(self._cache) > _MAX_CACHE_ENTRIES:
            evicted, _ = self._cache.popitem(last=False)
            log.debug("Evicted config cache entry for %s", evicted)

    def invalidate(self, owner: str, repo: str) -> None:
        self._cache.pop(f"{owner}/{repo}", None)

    def clear(self) -> None:
        self._cache.clear()

    def stats(self) -> dict[str, int]:
        """Cache counters, for the dashboard and for debugging live installs."""
        return {
            "entries": len(self._cache),
            "configured": sum(
                1 for entry in self._cache.values() if entry.config is not None
            ),
            "hits": self._hits,
            "misses": self._misses,
        }
