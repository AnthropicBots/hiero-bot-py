from __future__ import annotations

from typing import Any

from app.utils.settings import settings

from .base import (
    BackendPermanentError,
    BackendTransientError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)


class AnthropicBackend(ReviewBackend):
    """Anthropic Messages API backend."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or settings.anthropic_api_key
        self._client: Any = None

    @classmethod
    def available(cls) -> bool:
        return bool(settings.anthropic_api_key)

    def _get_client(self, timeout_seconds: float | None = None):
        if self._client is None:
            if not self._api_key:
                raise BackendUnavailable("ANTHROPIC_API_KEY is not set")

            try:
                import anthropic
            except ImportError as exc:
                raise BackendUnavailable(
                    "The anthropic package is not installed"
                ) from exc

            if timeout_seconds is None:
                self._client = anthropic.AsyncAnthropic(
                    api_key=self._api_key,
                    max_retries=0,
                )
            else:
                self._client = anthropic.AsyncAnthropic(
                    api_key=self._api_key,
                    max_retries=0,
                    timeout=float(timeout_seconds),
                )

        return self._client

    async def complete(self, request: CompletionRequest) -> str:
        client = self._get_client(request.timeout_seconds)

        message_kwargs = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
        }

        if request.temperature is not None:
            message_kwargs["temperature"] = request.temperature

        try:
            response = await client.messages.create(**message_kwargs)
        except Exception as exc:
            import anthropic

            if isinstance(
                exc,
                (
                    anthropic.APIConnectionError,
                    anthropic.APITimeoutError,
                ),
            ):
                raise BackendTransientError(
                    f"Anthropic transient request failure: {exc}"
                ) from exc

            if isinstance(exc, anthropic.APIStatusError):
                if exc.status_code in {408, 409, 429} or exc.status_code >= 500:
                    raise BackendTransientError(
                        f"Anthropic transient HTTP {exc.status_code}: {exc}"
                    ) from exc

                raise BackendPermanentError(
                    f"Anthropic HTTP {exc.status_code}: {exc}"
                ) from exc

            raise BackendPermanentError(
                f"Anthropic request failed: {exc}"
            ) from exc

        blocks = getattr(response, "content", None) or []
        text = "".join(getattr(block, "text", "") for block in blocks)

        if not text:
            raise BackendPermanentError("Anthropic returned an empty response")

        return text

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None