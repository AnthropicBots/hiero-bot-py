# app/ai/backends/anthropic_backend.py

from __future__ import annotations

from app.ai.backends.base import (
    BackendError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("ai.backend.anthropic")


class AnthropicBackend(ReviewBackend):
    """Anthropic Messages API via the official SDK."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self._client = None

    @classmethod
    def available(cls) -> bool:
        return bool(settings.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise BackendUnavailable("ANTHROPIC_API_KEY is not set")
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - packaging concern
                raise BackendUnavailable(
                    "The anthropic package is not installed"
                ) from exc

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def complete(self, request: CompletionRequest) -> str:
        client = self._get_client()

        try:
            response = await client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
            )
        except Exception as exc:
            raise BackendError(f"Anthropic request failed: {exc}") from exc

        blocks = getattr(response, "content", None) or []
        text = "".join(getattr(block, "text", "") for block in blocks)
        if not text:
            raise BackendError("Anthropic returned an empty response")
        return text
