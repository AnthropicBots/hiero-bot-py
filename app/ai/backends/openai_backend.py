# app/ai/backends/openai_backend.py

from __future__ import annotations

from typing import Any

from app.utils.logger import get_logger
from app.utils.settings import settings

from .base import (
    BackendPermanentError,
    BackendTransientError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)

log = get_logger("ai.backend.openai")


class OpenAICompatibleBackend(ReviewBackend):
    """
    Any OpenAI-compatible chat-completions endpoint.

    This covers OpenAI itself and, through OPENAI_BASE_URL, self-hosted
    serving stacks such as vLLM, LM Studio, llama.cpp's server, TGI,
    or a gateway in front of several providers.

    Many OpenAI-compatible servers accept any non-empty API key, so an
    unset key is allowed when a base URL is configured.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._base_url = base_url or settings.openai_base_url
        self._client: Any = None

    @classmethod
    def available(cls) -> bool:
        """Return whether the OpenAI-compatible backend is configured."""
        return bool(
            settings.openai_api_key
            or settings.openai_base_url
        )

    def _get_client(
        self,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Create and cache the OpenAI async client."""
        if self._client is not None:
            return self._client

        if not (self._api_key or self._base_url):
            raise BackendUnavailable(
                "Set OPENAI_API_KEY or OPENAI_BASE_URL "
                "to use this backend"
            )

        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise BackendUnavailable(
                "The openai package is not installed"
            ) from exc

        client_kwargs: dict[str, Any] = {
            "api_key": self._api_key or "not-needed",
            "max_retries": 0,
        }

        if self._base_url:
            client_kwargs["base_url"] = self._base_url

        if timeout_seconds is not None:
            client_kwargs["timeout"] = float(timeout_seconds)

        self._client = openai.AsyncOpenAI(
            **client_kwargs
        )

        return self._client

    async def complete(
        self,
        request: CompletionRequest,
    ) -> str:
        """Send a chat-completion request."""
        client = self._get_client(
            request.timeout_seconds
        )

        message_kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": request.system,
                },
                {
                    "role": "user",
                    "content": request.prompt,
                },
            ],
        }

        if request.temperature is not None:
            message_kwargs["temperature"] = request.temperature

        try:
            response = await client.chat.completions.create(
                **message_kwargs
            )

        except Exception as exc:
            try:
                import openai
            except ImportError as import_exc:  # pragma: no cover
                raise BackendUnavailable(
                    "The openai package is not installed"
                ) from import_exc

            if isinstance(
                exc,
                (
                    openai.APIConnectionError,
                    openai.APITimeoutError,
                ),
            ):
                raise BackendTransientError(
                    "OpenAI-compatible transient request failure: "
                    f"{exc}"
                ) from exc

            if isinstance(
                exc,
                openai.APIStatusError,
            ):
                status_code = exc.status_code

                if (
                    status_code in {408, 409, 429}
                    or status_code >= 500
                ):
                    raise BackendTransientError(
                        "OpenAI-compatible transient HTTP "
                        f"{status_code}: {exc}"
                    ) from exc

                raise BackendPermanentError(
                    "OpenAI-compatible HTTP "
                    f"{status_code}: {exc}"
                ) from exc

            raise BackendPermanentError(
                "OpenAI-compatible request failed: "
                f"{exc}"
            ) from exc

        choices = getattr(
            response,
            "choices",
            None,
        ) or []

        if not choices:
            raise BackendPermanentError(
                "OpenAI-compatible endpoint returned no choices"
            )

        message = getattr(
            choices[0],
            "message",
            None,
        )

        text = getattr(
            message,
            "content",
            None,
        )

        if not isinstance(text, str) or not text.strip():
            raise BackendPermanentError(
                "OpenAI-compatible endpoint returned empty content"
            )

        return text.strip()

    async def close(self) -> None:
        """Close the cached OpenAI client."""
        if self._client is not None:
            await self._client.close()
            self._client = None
