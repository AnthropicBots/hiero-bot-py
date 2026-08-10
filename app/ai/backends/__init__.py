# app/ai/backends — Pluggable model providers for AI review

from app.ai.backends.anthropic_backend import AnthropicBackend
from app.ai.backends.base import (
    BackendError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)
from app.ai.backends.ollama_backend import OllamaBackend
from app.ai.backends.openai_backend import OpenAICompatibleBackend
from app.ai.backends.registry import AUTO_ORDER, BACKENDS, build_backend

__all__ = [
    "AUTO_ORDER",
    "BACKENDS",
    "AnthropicBackend",
    "BackendError",
    "BackendUnavailable",
    "CompletionRequest",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "ReviewBackend",
    "build_backend",
]
