# app/ai/backends/registry.py — Backend selection

from __future__ import annotations

from app.ai.backends.anthropic_backend import AnthropicBackend
from app.ai.backends.base import BackendUnavailable, ReviewBackend
from app.ai.backends.ollama_backend import OllamaBackend
from app.ai.backends.openai_backend import OpenAICompatibleBackend
from app.utils.logger import get_logger

log = get_logger("ai.backend.registry")

BACKENDS: dict[str, type[ReviewBackend]] = {
    AnthropicBackend.name: AnthropicBackend,
    OpenAICompatibleBackend.name: OpenAICompatibleBackend,
    OllamaBackend.name: OllamaBackend,
}

# Order used when `provider: auto`. OpenAI-compatible comes first to preserve
# the precedence the reviewer already had (OPENAI_API_KEY overrode Anthropic),
# and Ollama is last because it is the fallback that needs no credentials.
AUTO_ORDER = (
    OpenAICompatibleBackend.name,
    AnthropicBackend.name,
    OllamaBackend.name,
)


def build_backend(provider: str = "auto") -> ReviewBackend:
    """
    Construct the backend named by `provider`.

    `auto` picks the first configured one in `AUTO_ORDER`. Naming a provider
    explicitly builds it even if `available()` is False, so a misconfiguration
    surfaces as a clear error from that backend rather than silently falling
    through to a different model than the maintainer asked for.
    """
    provider = (provider or "auto").lower()

    if provider == "auto":
        for name in AUTO_ORDER:
            backend_cls = BACKENDS[name]
            if backend_cls.available():
                log.debug("AI review auto-selected the %s backend", name)
                return backend_cls()

        raise BackendUnavailable(
            "AI review is enabled but no backend is configured. Set one of "
            "ANTHROPIC_API_KEY, OPENAI_API_KEY / OPENAI_BASE_URL, or OLLAMA_BASE_URL."
        )

    backend_cls = BACKENDS.get(provider)
    if backend_cls is None:
        raise BackendUnavailable(
            f"Unknown AI provider {provider!r}. "
            f"Supported: {', '.join(sorted(BACKENDS))}, or 'auto'."
        )

    return backend_cls()
