# tests/unit/test_config_loader.py — per-repo config loading (#43)

import asyncio
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
async def test_loads_wrapped_base64_config():
    encoded = encode(VALID_YAML)
    wrapped = "\n".join(encoded[i : i + 20] for i in range(0, len(encoded), 20))
    loader, _ = make_loader(wrapped)

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
    loader._client.get_file_content = AsyncMock(side_effect=[encode(VALID_YAML), None])

    await loader.load("hiero", "configured", 1)
    await loader.load("hiero", "silent", 1)

    stats = loader.stats()
    assert stats["entries"] == 2
    assert stats["configured"] == 1
    assert stats["misses"] == 2


TYPO_YAML = """
repo: "hiero/sdk-js"
workflows:
  pull_request:
    quality_gate:
      require_dco: false
"""


# ── In-flight coalescing (#100) ───────────────────────────────


async def _hold_fetch(started: asyncio.Event, release: asyncio.Event, value):
    started.set()
    await release.wait()
    return value


@pytest.mark.asyncio
async def test_concurrent_loads_share_one_github_request():
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(*args):
        return await _hold_fetch(started, release, encode(VALID_YAML))

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    pending = asyncio.gather(
        *[loader.load("hiero", "sdk-js", 42) for _ in range(10)]
    )
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await pending

    assert client.get_file_content.await_count == 1
    assert all(
        result is not None and result.repo == "hiero/sdk-js" for result in results
    )
    assert loader.stats()["misses"] == 1
    assert loader.stats()["hits"] == 0
    assert loader.stats()["coalesced"] == 9
    assert loader._in_flight == {}


@pytest.mark.asyncio
async def test_concurrent_missing_configs_share_one_request():
    started = asyncio.Event()
    release = asyncio.Event()
    async def fetch(*args):
        return await _hold_fetch(started, release, None)

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    pending = asyncio.gather(
        *[loader.load("hiero", "sdk-js", 42) for _ in range(10)]
    )
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await pending

    assert client.get_file_content.await_count == 1
    assert results == [None] * 10
    assert loader._in_flight == {}


@pytest.mark.asyncio
async def test_concurrent_failures_share_one_request_and_clear_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()

    async def fail(*args):
        started.set()
        await release.wait()
        raise http_status_error(500)

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fail)
    loader = ConfigLoader(client)

    pending = asyncio.gather(
        *[loader.load("hiero", "sdk-js", 42) for _ in range(10)],
        return_exceptions=True,
    )
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await pending

    assert client.get_file_content.await_count == 1
    assert all(isinstance(result, httpx.HTTPStatusError) for result in results)
    assert loader._in_flight == {}

    with pytest.raises(httpx.HTTPStatusError):
        await loader.load("hiero", "sdk-js", 42)
    assert client.get_file_content.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_loads_of_different_repos_are_not_coalesced():
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def fetch(owner, repo, *args):
        started[sum(event.is_set() for event in started)].set()
        await release.wait()
        if repo == "sdk-js":
            return encode(VALID_YAML)
        if repo == "sdk-python":
            return encode(FRESH_YAML)
        raise AssertionError(f"Unexpected repository: {repo}")

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    pending = asyncio.gather(
        loader.load("hiero", "sdk-js", 1),
        loader.load("hiero", "sdk-python", 1),
    )

    await asyncio.gather(*(event.wait() for event in started))
    assert client.get_file_content.await_count == 2

    release.set()
    results = await pending

    assert results[0] is not None
    assert results[0].repo == "hiero/sdk-js"
    assert results[1] is not None
    assert results[1].repo == "hiero/sdk-python"
    assert loader._in_flight == {}
    assert loader._live == {}


FRESH_YAML = """
repo: "hiero/sdk-python"
workflows:
  onboarding:
    enabled: true
"""


async def _blocked_then(release: asyncio.Event, started: asyncio.Event, value):
    started.set()
    await release.wait()
    return value


@pytest.mark.asyncio
async def test_invalidate_during_fetch_does_not_republish_stale_config():
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    calls = {"n": 0}

    async def fetch(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            return await _blocked_then(release_first, first_started, encode(VALID_YAML))
        return encode(FRESH_YAML)

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    first = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await first_started.wait()
    loader.invalidate("hiero", "sdk-js")
    second = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await asyncio.sleep(0)
    release_first.set()
    stale, fresh = await asyncio.gather(first, second)

    assert calls["n"] == 2
    assert stale.repo == "hiero/sdk-js"
    assert fresh.repo == "hiero/sdk-python"
    assert loader._cache["hiero/sdk-js"].config.repo == "hiero/sdk-python"
    cached = await loader.load("hiero", "sdk-js", 42)
    assert cached.repo == "hiero/sdk-python"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_invalidate_during_missing_fetch_does_not_install_negative_cache():
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    calls = {"n": 0}

    async def fetch(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            return await _blocked_then(release_first, first_started, None)
        return encode(FRESH_YAML)

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    first = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await first_started.wait()
    loader.invalidate("hiero", "sdk-js")
    second = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await asyncio.sleep(0)
    release_first.set()
    stale, fresh = await asyncio.gather(first, second)

    assert stale is None
    assert fresh is not None and fresh.repo == "hiero/sdk-python"
    assert loader._cache["hiero/sdk-js"].config is not None
    cached = await loader.load("hiero", "sdk-js", 42)
    assert cached.repo == "hiero/sdk-python"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_clear_during_fetch_does_not_repopulate_cache():
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    calls = {"n": 0}

    async def fetch(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            return await _blocked_then(release_first, first_started, encode(VALID_YAML))
        return encode(FRESH_YAML)

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    first = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await first_started.wait()
    loader.clear()
    second = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first, second)

    assert loader._cache["hiero/sdk-js"].config.repo == "hiero/sdk-python"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_idle_invalidations_do_not_retain_generation_entries():
    loader, _ = make_loader(encode(VALID_YAML))

    for index in range(20):
        await loader.load("hiero", f"repo-{index}", 1)
        loader.invalidate("hiero", f"repo-{index}")

    assert loader._generation == {}
    assert loader._live == {}


@pytest.mark.asyncio
async def test_generation_is_kept_only_while_a_fetch_is_in_flight():
    release = asyncio.Event()
    started = asyncio.Event()

    async def fetch(*args):
        return await _blocked_then(release, started, encode(VALID_YAML))

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    pending = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await started.wait()
    loader.invalidate("hiero", "sdk-js")
    assert "hiero/sdk-js" in loader._generation

    release.set()
    await pending

    assert "hiero/sdk-js" not in loader._generation
    assert loader._live == {}


@pytest.mark.asyncio
async def test_cancelling_the_leader_cancels_waiters_and_clears_in_flight():
    started = asyncio.Event()

    async def fetch(*args):
        started.set()
        await asyncio.Event().wait()

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    leader = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await started.wait()
    await asyncio.sleep(0)
    waiters = [
        asyncio.create_task(loader.load("hiero", "sdk-js", 42)) for _ in range(3)
    ]
    await asyncio.sleep(0)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert loader._in_flight == {}
    assert loader._live == {}
    assert loader._generation == {}


INVALID_YAML = "- just\n- a\n- list\n"

THIRD_YAML = """
repo: "hiero/sdk-go"
workflows:
  onboarding:
    enabled: true
"""


@pytest.mark.asyncio
async def test_concurrent_invalid_configs_share_one_request_and_retry():
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(*args):
        return await _blocked_then(release, started, encode(INVALID_YAML))

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    pending = asyncio.gather(
        *[loader.load("hiero", "sdk-js", 42) for _ in range(10)],
        return_exceptions=True,
    )
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await pending

    assert client.get_file_content.await_count == 1
    assert all(isinstance(result, ConfigInvalid) for result in results)
    assert loader._in_flight == {}
    assert loader._live == {}

    with pytest.raises(ConfigInvalid):
        await loader.load("hiero", "sdk-js", 42)
    assert client.get_file_content.await_count == 2


@pytest.mark.asyncio
async def test_successive_invalidations_ignore_stale_generations():
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    payloads = [encode(VALID_YAML), encode(FRESH_YAML), encode(THIRD_YAML)]
    calls = {"n": 0}

    async def fetch(*args):
        index = calls["n"]
        calls["n"] += 1
        started[index].set()
        await releases[index].wait()
        return payloads[index]

    client = Mock()
    client.get_file_content = AsyncMock(side_effect=fetch)
    loader = ConfigLoader(client)

    oldest = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await started[0].wait()
    loader.invalidate("hiero", "sdk-js")

    middle = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await started[1].wait()
    loader.invalidate("hiero", "sdk-js")

    newest = asyncio.create_task(loader.load("hiero", "sdk-js", 42))
    await started[2].wait()

    assert calls["n"] == 3
    assert loader._live["hiero/sdk-js"] == 3

    releases[1].set()
    await middle
    assert "hiero/sdk-js" not in loader._cache

    releases[0].set()
    await oldest
    assert "hiero/sdk-js" not in loader._cache

    releases[2].set()
    fresh = await newest

    assert fresh.repo == "hiero/sdk-go"
    assert loader._cache["hiero/sdk-js"].config.repo == "hiero/sdk-go"
    assert loader._in_flight == {}
    assert loader._live == {}
    assert loader._generation == {}

    cached = await loader.load("hiero", "sdk-js", 42)
    assert cached.repo == "hiero/sdk-go"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_unknown_keys_are_reported_but_do_not_invalidate_the_config(monkeypatch):
    log = Mock()
    monkeypatch.setattr(loader_module, "log", log)

    loader, _ = make_loader(encode(TYPO_YAML))
    config = await loader.load("hiero", "sdk-js", 1)

    assert config is not None
    warned = " ".join(
        str(arg) for call in log.warning.call_args_list for arg in call.args
    )
    assert "workflows.pull_request.quality_gate" in warned
