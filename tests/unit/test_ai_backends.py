# tests/unit/test_ai_backends.py — pluggable model providers

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.ai.backends import (
    AUTO_ORDER,
    BACKENDS,
    AnthropicBackend,
    BackendError,
    BackendUnavailable,
    CompletionRequest,
    OllamaBackend,
    OpenAICompatibleBackend,
    ReviewBackend,
    build_backend,
)
from app.utils.settings import settings

REQUEST = CompletionRequest(
    system="You are a reviewer.",
    prompt="Review this diff.",
    model="test-model",
    max_tokens=256,
)


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "openai_base_url", None)
    monkeypatch.setattr(settings, "ollama_base_url", None)


# ── Registry ──────────────────────────────────────────────────


def test_every_registered_backend_implements_the_interface():
    for name, backend_cls in BACKENDS.items():
        assert issubclass(backend_cls, ReviewBackend)
        assert backend_cls.name == name
        assert not backend_cls.__abstractmethods__


def test_auto_selects_openai_first(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-x")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")

    assert build_backend("auto").name == "openai"


def test_auto_falls_back_to_anthropic(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")

    assert build_backend("auto").name == "anthropic"


def test_auto_falls_back_to_a_local_model(monkeypatch):
    monkeypatch.setattr(settings, "ollama_base_url", "http://localhost:11434")

    assert build_backend("auto").name == "ollama"


def test_auto_order_prefers_hosted_then_local():
    assert AUTO_ORDER == ("openai", "anthropic", "ollama")


def test_auto_with_nothing_configured_explains_the_options():
    with pytest.raises(BackendUnavailable) as exc:
        build_backend("auto")

    message = str(exc.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "OLLAMA_BASE_URL" in message


def test_named_provider_is_built_even_when_unconfigured():
    """Silently reviewing with a different model than asked for is worse."""
    assert build_backend("ollama").name == "ollama"


def test_unknown_provider_is_rejected():
    with pytest.raises(BackendUnavailable, match="Unknown AI provider"):
        build_backend("gpt-9000")


def test_provider_name_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    assert build_backend("Anthropic").name == "anthropic"


def test_empty_provider_is_treated_as_auto(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    assert build_backend("").name == "anthropic"


# ── Availability ──────────────────────────────────────────────


def test_availability_reflects_configuration(monkeypatch):
    assert AnthropicBackend.available() is False
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    assert AnthropicBackend.available() is True


def test_openai_backend_is_available_with_only_a_base_url(monkeypatch):
    """Self-hosted OpenAI-compatible servers usually ignore the key."""
    monkeypatch.setattr(settings, "openai_base_url", "http://localhost:8000/v1")
    assert OpenAICompatibleBackend.available() is True


def test_ollama_availability_needs_an_endpoint(monkeypatch):
    assert OllamaBackend.available() is False
    monkeypatch.setattr(settings, "ollama_base_url", "http://localhost:11434")
    assert OllamaBackend.available() is True


# ── Anthropic backend ─────────────────────────────────────────


def anthropic_client(text=None, error=None):
    client = MagicMock()
    if error is not None:
        client.messages.create = AsyncMock(side_effect=error)
    else:
        block = MagicMock()
        block.text = text
        response = MagicMock()
        response.content = [block]
        client.messages.create = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_anthropic_returns_text():
    backend = AnthropicBackend(api_key="sk-ant")
    backend._client = anthropic_client(text='{"verdict":"approve"}')

    assert await backend.complete(REQUEST) == '{"verdict":"approve"}'


@pytest.mark.asyncio
async def test_anthropic_passes_system_prompt_separately():
    backend = AnthropicBackend(api_key="sk-ant")
    client = anthropic_client(text="ok")
    backend._client = client

    await backend.complete(REQUEST)

    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["system"] == REQUEST.system
    assert kwargs["messages"][0]["content"] == REQUEST.prompt
    assert kwargs["model"] == "test-model"


@pytest.mark.asyncio
async def test_anthropic_errors_become_backend_errors():
    backend = AnthropicBackend(api_key="sk-ant")
    backend._client = anthropic_client(error=RuntimeError("429"))

    with pytest.raises(BackendError, match="Anthropic request failed"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_anthropic_empty_response_is_an_error():
    backend = AnthropicBackend(api_key="sk-ant")
    backend._client = anthropic_client(text="")

    with pytest.raises(BackendError, match="empty"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_anthropic_without_a_key_is_unavailable():
    with pytest.raises(BackendUnavailable, match="ANTHROPIC_API_KEY"):
        await AnthropicBackend(api_key=None).complete(REQUEST)


# ── OpenAI-compatible backend ─────────────────────────────────


def openai_client(content=None, error=None, choices=None):
    client = MagicMock()
    if error is not None:
        client.chat.completions.create = AsyncMock(side_effect=error)
        return client

    if choices is None:
        message = MagicMock()
        message.content = content
        choice = MagicMock()
        choice.message = message
        choices = [choice]

    response = MagicMock()
    response.choices = choices
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_openai_returns_message_content():
    backend = OpenAICompatibleBackend(api_key="sk-x")
    backend._client = openai_client(content='{"verdict":"comment"}')

    assert await backend.complete(REQUEST) == '{"verdict":"comment"}'


@pytest.mark.asyncio
async def test_openai_sends_system_as_a_message():
    backend = OpenAICompatibleBackend(api_key="sk-x")
    client = openai_client(content="ok")
    backend._client = client

    await backend.complete(REQUEST)

    messages = client.chat.completions.create.await_args.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": REQUEST.system}
    assert messages[1] == {"role": "user", "content": REQUEST.prompt}


@pytest.mark.asyncio
async def test_openai_errors_become_backend_errors():
    backend = OpenAICompatibleBackend(api_key="sk-x")
    backend._client = openai_client(error=RuntimeError("timeout"))

    with pytest.raises(BackendError):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_openai_no_choices_is_an_error():
    backend = OpenAICompatibleBackend(api_key="sk-x")
    backend._client = openai_client(choices=[])

    with pytest.raises(BackendError, match="no choices"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_openai_unconfigured_is_unavailable():
    with pytest.raises(BackendUnavailable, match="OPENAI_API_KEY"):
        await OpenAICompatibleBackend(api_key=None, base_url=None).complete(REQUEST)


# ── Ollama backend ────────────────────────────────────────────


def ollama_backend(handler):
    client = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(handler)
    )
    return OllamaBackend(base_url="http://localhost:11434", client=client)


@pytest.mark.asyncio
async def test_ollama_returns_message_content():
    backend = ollama_backend(
        lambda request: httpx.Response(
            200, json={"message": {"content": '{"verdict":"approve"}'}}
        )
    )

    assert await backend.complete(REQUEST) == '{"verdict":"approve"}'


@pytest.mark.asyncio
async def test_ollama_requests_a_non_streaming_chat():
    import json as json_module

    seen = {}

    def handler(request):
        seen.update(json_module.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    await ollama_backend(handler).complete(REQUEST)

    assert seen["stream"] is False
    assert seen["model"] == "test-model"
    assert seen["options"]["num_predict"] == 256
    assert seen["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_ollama_missing_model_says_to_pull_it():
    backend = ollama_backend(lambda request: httpx.Response(404))

    with pytest.raises(BackendError, match="pull it first"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_ollama_server_error_is_reported():
    backend = ollama_backend(lambda request: httpx.Response(500, text="oom"))

    with pytest.raises(BackendError, match="500"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_ollama_empty_message_is_an_error():
    backend = ollama_backend(
        lambda request: httpx.Response(200, json={"message": {"content": ""}})
    )

    with pytest.raises(BackendError, match="empty"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_ollama_non_json_body_is_an_error():
    backend = ollama_backend(lambda request: httpx.Response(200, text="not json"))

    with pytest.raises(BackendError, match="non-JSON"):
        await backend.complete(REQUEST)


@pytest.mark.asyncio
async def test_ollama_transport_failure_is_an_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(BackendError, match="Ollama request failed"):
        await ollama_backend(handler).complete(REQUEST)


@pytest.mark.asyncio
async def test_ollama_without_an_endpoint_is_unavailable():
    with pytest.raises(BackendUnavailable, match="OLLAMA_BASE_URL"):
        await OllamaBackend(base_url="").complete(REQUEST)


@pytest.mark.asyncio
async def test_ollama_does_not_close_a_borrowed_client():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    backend = OllamaBackend(base_url="http://localhost:11434", client=client)

    await backend.close()

    assert client.is_closed is False
    await client.aclose()
