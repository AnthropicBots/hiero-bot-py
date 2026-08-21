# tests/unit/test_config_loader.py — per-repo config loading (#43)

import base64
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.config import loader as loader_module
from app.config.loader import ConfigInvalid, ConfigLoader

VALID_YAML = """
repo: "hiero/sdk-js"
workflows:
  onboarding:
    enabled: true
"""


def encode(text):
    return base64.b64encode(text.encode()).decode()


def make_loader(content=None, side_effect=None):
    client = Mock()
    if side_effect is not None:
        client.get_file_content = AsyncMock(side_effect=side_effect)
    else:
        client.get_file_content = AsyncMock(return_value=content)
    return ConfigLoader(client), client


def http_status_error(status):
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ── Happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loads_and_validates_config():
    loader, _ = make_loader(encode(VALID_YAML))

    config = await loader.load("hiero", "sdk-js", 42)

    assert config is not None
    assert config.repo == "hiero/sdk-js"


@pytest.mark.asyncio
async def test_installation_id_is_passed_through():
    loader, client = make_loader(encode(VALID_YAML))

    await loader.load("hiero", "sdk-js", 42)

    client.get_file_content.assert_awaited_once_with(
        "hiero", "sdk-js", ".github/hiero-bot.yml", 42
    )


@pytest.mark.asyncio
async def test_second_load_is_served_from_cache():
    loader, client = make_loader(encode(VALID_YAML))

    await loader.load("hiero", "sdk-js", 42)
    await loader.load("hiero", "sdk-js", 42)

    assert client.get_file_content.await_count == 1
    assert loader.stats()["hits"] == 1


@pytest.mark.asyncio
async def test_invalidate_forces_a_refetch():
    loader, client = make_loader(encode(VALID_YAML))

    await loader.load("hiero", "sdk-js", 42)
    loader.invalidate("hiero", "sdk-js")
    await loader.load("hiero", "sdk-js", 42)

    assert client.get_file_content.await_count == 2


@pytest.mark.asyncio
async def test_clear_empties_the_cache():
    loader, _ = make_loader(encode(VALID_YAML))

    await loader.load("hiero", "sdk-js", 42)
    loader.clear()

    assert loader.stats()["entries"] == 0


# ── Missing config ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_config_returns_none():
    loader, _ = make_loader(None)

    assert await loader.load("hiero", "sdk-js", 42) is None


@pytest.mark.asyncio
async def test_missing_config_is_negatively_cached():
    """Every webhook from an unconfigured repo used to re-hit the contents API."""
    loader, client = make_loader(None)

    await loader.load("hiero", "sdk-js", 42)
    await loader.load("hiero", "sdk-js", 42)

    assert client.get_file_content.await_count == 1


@pytest.mark.asyncio
async def test_negative_cache_expires_sooner_than_a_hit(monkeypatch):
    monkeypatch.setattr(loader_module, "_NEGATIVE_CACHE_TTL", 0)
    loader, client = make_loader(None)

    await loader.load("hiero", "sdk-js", 42)
    await loader.load("hiero", "sdk-js", 42)

    assert client.get_file_content.await_count == 2


@pytest.mark.asyncio
async def test_404_from_a_lower_layer_disables_the_bot():
    """Regression for #43 — this branch could never fire, so a 404 became a 500."""
    loader, _ = make_loader(side_effect=http_status_error(404))

    assert await loader.load("hiero", "sdk-js", 42) is None


@pytest.mark.asyncio
async def test_non_404_http_errors_still_propagate():
    loader, _ = make_loader(side_effect=http_status_error(500))

    with pytest.raises(httpx.HTTPStatusError):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_http_failure_is_not_cached():
    loader, client = make_loader(side_effect=http_status_error(500))

    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await loader.load("hiero", "sdk-js", 42)

    assert client.get_file_content.await_count == 2


# ── Broken config ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_yaml_raises_config_invalid():
    loader, _ = make_loader(encode("repo: [unclosed\n"))

    with pytest.raises(ConfigInvalid, match="YAML syntax error"):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_schema_violation_names_the_field():
    loader, _ = make_loader(encode('repo: "not-a-slug"\n'))

    with pytest.raises(ConfigInvalid) as exc_info:
        await loader.load("hiero", "sdk-js", 42)

    assert "repo" in exc_info.value.detail
    assert exc_info.value.slug == "hiero/sdk-js"


@pytest.mark.asyncio
async def test_empty_file_is_rejected():
    loader, _ = make_loader(encode("\n"))

    with pytest.raises(ConfigInvalid, match="empty"):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_non_mapping_root_is_rejected():
    loader, _ = make_loader(encode("- just\n- a\n- list\n"))

    with pytest.raises(ConfigInvalid, match="must be a mapping"):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_oversized_config_is_rejected(monkeypatch):
    monkeypatch.setattr(loader_module, "_MAX_CONFIG_BYTES", 32)
    loader, _ = make_loader(encode(VALID_YAML))

    with pytest.raises(ConfigInvalid, match="over the"):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_non_utf8_content_is_rejected():
    loader, _ = make_loader(base64.b64encode(b"\xff\xfe\x00bad").decode())

    with pytest.raises(ConfigInvalid, match="UTF-8"):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_broken_base64_is_rejected():
    loader, _ = make_loader("!!!not base64!!!")

    with pytest.raises(ConfigInvalid, match="base64"):
        await loader.load("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_invalid_config_is_not_cached():
    loader, client = make_loader(encode("- list\n"))

    for _ in range(2):
        with pytest.raises(ConfigInvalid):
            await loader.load("hiero", "sdk-js", 42)

    assert client.get_file_content.await_count == 2


# ── Cache bounds ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(loader_module, "_MAX_CACHE_ENTRIES", 2)
    loader, _ = make_loader(encode(VALID_YAML))

    await loader.load("hiero", "a", 1)
    await loader.load("hiero", "b", 1)
    await loader.load("hiero", "a", 1)  # refresh recency of "a"
    await loader.load("hiero", "c", 1)

    assert loader.stats()["entries"] == 2
    assert "hiero/b" not in loader._cache
    assert "hiero/a" in loader._cache


@pytest.mark.asyncio
async def test_stats_separate_configured_from_silent_repos():
    loader = ConfigLoader(Mock())
    loader._client.get_file_content = AsyncMock(
        side_effect=[encode(VALID_YAML), None]
    )

    await loader.load("hiero", "configured", 1)
    await loader.load("hiero", "silent", 1)

    stats = loader.stats()
    assert stats["entries"] == 2
    assert stats["configured"] == 1
    assert stats["misses"] == 2
