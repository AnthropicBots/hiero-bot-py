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

# Order used when `provider: auto`. The first backend whose `available()`
# check succeeds is selected. Keep this order explicit so adding a backend
# does not silently change which provider auto-selection prefers.
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
        available = [
            name for name in AUTO_ORDER if BACKENDS[name].available()
        ]

        if len(available) > 1:
            log.warning(
                "Multiple AI review backends are available: %s. "
                "Auto-selection will use registry priority: %s",
                ", ".join(available),
                available[0],
            )

        if available:
            name = available[0]
            log.info("AI review auto-selected the %s backend", name)
            return BACKENDS[name]()

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
