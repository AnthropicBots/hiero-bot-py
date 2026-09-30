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
        """Return whether the Anthropic backend is configured."""
        return bool(settings.anthropic_api_key)

    def _get_client(
        self,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Create and cache the Anthropic async client."""
        if self._client is not None:
            return self._client

        if not self._api_key:
            raise BackendUnavailable(
                "ANTHROPIC_API_KEY is not set"
            )

        try:
            import anthropic
        except ImportError as exc:
            raise BackendUnavailable(
                "The anthropic package is not installed"
            ) from exc

        client_kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "max_retries": 0,
        }

        if timeout_seconds is not None:
            client_kwargs["timeout"] = float(timeout_seconds)

        self._client = anthropic.AsyncAnthropic(
            **client_kwargs
        )

        return self._client

    async def complete(
        self,
        request: CompletionRequest,
    ) -> str:
        """Send a completion request through the Anthropic Messages API."""
        client = self._get_client(
            request.timeout_seconds
        )

        message_kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [
                {
                    "role": "user",
                    "content": request.prompt,
                }
            ],
        }

        if request.temperature is not None:
            message_kwargs["temperature"] = request.temperature

        try:
            response = await client.messages.create(
                **message_kwargs
            )

        except Exception as exc:
            # Import here so the backend can still be imported when the
            # optional anthropic dependency is not installed.
            try:
                import anthropic
            except ImportError as import_exc:
                raise BackendUnavailable(
                    "The anthropic package is not installed"
                ) from import_exc

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

            if isinstance(
                exc,
                anthropic.APIStatusError,
            ):
                status_code = exc.status_code

                if (
                    status_code in {408, 409, 429}
                    or status_code >= 500
                ):
                    raise BackendTransientError(
                        f"Anthropic transient HTTP "
                        f"{status_code}: {exc}"
                    ) from exc

                raise BackendPermanentError(
                    f"Anthropic HTTP "
                    f"{status_code}: {exc}"
                ) from exc

            raise BackendPermanentError(
                f"Anthropic request failed: {exc}"
            ) from exc

        blocks = getattr(
            response,
            "content",
            None,
        ) or []

        text_parts: list[str] = []

        for block in blocks:
            block_text = getattr(
                block,
                "text",
                None,
            )

            if isinstance(block_text, str):
                text_parts.append(block_text)

        text = "".join(text_parts).strip()

        if not text:
            raise BackendPermanentError(
                "Anthropic returned an empty response"
            )

        return text

    async def close(self) -> None:
        """Close the cached Anthropic client."""
        if self._client is not None:
            await self._client.close()
            self._client = None
