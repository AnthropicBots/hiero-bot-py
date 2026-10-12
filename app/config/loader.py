# app/config/loader.py — Per-repo YAML config loader with TTL cache

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import yaml
from pydantic import ValidationError

from app.config.schema import RepoConfig, find_unknown_keys
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.github.client import GitHubClient


log = get_logger("config.loader")

_CONFIG_PATH = ".github/hiero-bot.yml"

# A repo with a config is the common case and its contents change rarely.
_CACHE_TTL = 300  # 5 minutes

# "No config" is cached too, but far more briefly so a newly added config
# file is discovered without repeatedly hitting the GitHub Contents API.
_NEGATIVE_CACHE_TTL = 60

# Bound the cache so an org-wide installation cannot grow it without limit.
_MAX_CACHE_ENTRIES = 512

# A bot config is normally only a few kilobytes.
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


@dataclass
class _InFlight:
    """One contents-API fetch shared by callers of the same cache generation."""

    generation: int
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: RepoConfig | None = None
    error: BaseException | None = None


class ConfigLoader:
    def __init__(self, github_client: GitHubClient) -> None:
        self._client = github_client
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._in_flight: dict[str, _InFlight] = {}
        # How many fetches for this key have not finished yet, including ones
        # that invalidate() replaced in `_in_flight`. Generation entries exist
        # only while that count is non-zero, so the map stays bounded by the
        # number of repos with a request in flight.
        self._live: dict[str, int] = {}
        # Bumped by invalidate() and clear() so a fetch that started earlier
        # cannot republish its result into the cache.
        self._generation: dict[str, int] = {}
        self._hits = 0
        self._misses = 0
        self._coalesced = 0

    async def load(
        self,
        owner: str,
        repo: str,
        installation_id: int = 0,
    ) -> RepoConfig | None:
        """
        Load a repository's config, using the cache when fresh.

        Returns None when the repository has no config file. The bot stays
        silent in that case. Concurrent misses for the same repo share one
        GitHub request. A caller that arrives while a fetch is running waits
        for it instead of starting another one.

        Raises:
            ConfigInvalid: If the config exists but cannot be parsed or
                validated.
            httpx.HTTPStatusError: For unexpected GitHub API failures.
        """
        key = f"{owner}/{repo}"

        hit, config = self._read_cache(key)
        if hit:
            return config

        # No await between the miss check and registration, so two coroutines
        # cannot both decide they are the leader for this generation.
        generation = self._generation.get(key, 0)
        flight = self._in_flight.get(key)
        if flight is None or flight.generation != generation:
            flight = _InFlight(generation=generation)
            self._in_flight[key] = flight
            self._live[key] = self._live.get(key, 0) + 1
            completed = False
            try:
                self._misses += 1
                config = await self._fetch(
                    owner, repo, installation_id, key, generation
                )
                flight.result = config
                completed = True
                return config
            except asyncio.CancelledError as exc:
                flight.error = exc
                raise
            except Exception as exc:
                flight.error = exc
                raise
            finally:
                self._finish_flight(key, flight, completed)

        await flight.event.wait()
        self._coalesced += 1
        if flight.error is not None:
            raise flight.error
        return flight.result

    def _read_cache(self, key: str) -> tuple[bool, RepoConfig | None]:
        entry = self._cache.get(key)
        if entry is None or not entry.fresh:
            return False, None
        self._hits += 1
        self._cache.move_to_end(key)
        return True, entry.config

    async def _fetch(
        self,
        owner: str,
        repo: str,
        installation_id: int,
        key: str,
        generation: int,
    ) -> RepoConfig | None:
        try:
            raw_b64 = await self._client.get_file_content(
                owner,
                repo,
                _CONFIG_PATH,
                installation_id,
            )
        except httpx.HTTPStatusError as exc:
            # get_file_content normally converts 404 into None, but handle
            # a 404 defensively in case another layer lets it through.
            if exc.response.status_code == 404:
                log.debug("No config for %s — bot disabled", key)
                self._store_if_current(key, None, generation)
                return None

            log.error("Failed loading config for %s: %s", key, exc)
            raise

        if raw_b64 is None:
            log.debug("No config for %s — bot disabled", key)
            self._store_if_current(key, None, generation)
            return None

        config = self._parse(key, raw_b64)
        if self._store_if_current(key, config, generation):
            log.info("Loaded config for %s", key)
        return config

    # ── Parsing ───────────────────────────────────────────────

    @staticmethod
    def _parse(slug: str, raw_b64: str) -> RepoConfig:
        try:
            raw = base64.b64decode(raw_b64)
        except (binascii.Error, ValueError) as exc:
            raise ConfigInvalid(
                slug,
                f"content is not valid base64 ({exc})",
            ) from exc

        if len(raw) > _MAX_CONFIG_BYTES:
            raise ConfigInvalid(
                slug,
                f"file is {len(raw)} bytes, over the {_MAX_CONFIG_BYTES}-byte limit",
            )

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigInvalid(
                slug,
                "file is not valid UTF-8",
            ) from exc

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigInvalid(
                slug,
                f"YAML syntax error ({exc})",
            ) from exc

        if data is None:
            raise ConfigInvalid(slug, "file is empty")

        if not isinstance(data, dict):
            raise ConfigInvalid(
                slug,
                f"top level must be a mapping, got {type(data).__name__}",
            )

        try:
            config = RepoConfig.model_validate(data)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:5]
            )

            log.error(
                "Invalid config for %s: %s",
                slug,
                detail,
            )

            raise ConfigInvalid(slug, detail) from exc

        # Unknown keys are intentionally non-fatal so existing configs remain
        # compatible. Warn because a typo can otherwise silently activate a
        # default value.
        unknown = find_unknown_keys(data, RepoConfig)

        if unknown:
            log.warning(
                "Ignoring unknown keys in the config for %s: %s",
                slug,
                ", ".join(unknown),
            )

        return config

    # ── Cache management ──────────────────────────────────────

    def _store(
        self,
        key: str,
        config: RepoConfig | None,
    ) -> None:
        ttl = _CACHE_TTL if config is not None else _NEGATIVE_CACHE_TTL

        self._cache[key] = _CacheEntry(
            config=config,
            expires_at=time.monotonic() + ttl,
        )

        self._cache.move_to_end(key)

        while len(self._cache) > _MAX_CACHE_ENTRIES:
            evicted, _ = self._cache.popitem(last=False)
            log.debug("Evicted config cache entry for %s", evicted)

    def _store_if_current(
        self, key: str, config: RepoConfig | None, generation: int
    ) -> bool:
        """Store only when this fetch still belongs to the current generation."""
        if self._generation.get(key, 0) != generation:
            log.debug("Dropping stale config fetch for %s", key)
            return False
        self._store(key, config)
        return True

    def _bump(self, key: str) -> None:
        self._generation[key] = self._generation.get(key, 0) + 1

    def _forget_generation_if_idle(self, key: str) -> None:
        if not self._live.get(key):
            self._generation.pop(key, None)

    def _finish_flight(self, key: str, flight: _InFlight, completed: bool) -> None:
        # A leader cancelled before it recorded an error must not look like a
        # successful empty config. Propagate CancelledError so waiters keep
        # cancellation semantics instead of seeing a normal fetch failure.
        if not completed and flight.error is None:
            flight.error = asyncio.CancelledError()
        flight.event.set()
        if self._in_flight.get(key) is flight:
            del self._in_flight[key]
        remaining = self._live.get(key, 0) - 1
        if remaining > 0:
            self._live[key] = remaining
        else:
            self._live.pop(key, None)
            self._generation.pop(key, None)

    def invalidate(self, owner: str, repo: str) -> None:
        """Invalidate one repository's cached configuration."""
        key = f"{owner}/{repo}"
        self._cache.pop(key, None)
        self._bump(key)
        self._forget_generation_if_idle(key)

    def clear(self) -> None:
        """Clear all cached configuration entries."""
        self._cache.clear()
        for key in set(self._generation) | set(self._live):
            self._bump(key)
            self._forget_generation_if_idle(key)

    def stats(self) -> dict[str, int]:
        """Return cache statistics for monitoring and debugging."""
        return {
            "entries": len(self._cache),
            "configured": sum(
                1 for entry in self._cache.values() if entry.config is not None
            ),
            "hits": self._hits,
            "misses": self._misses,
            "coalesced": self._coalesced,
        }
