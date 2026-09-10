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

    That covers OpenAI itself and, via `OPENAI_BASE_URL`, self-hosted
    open-weight serving stacks — vLLM, LM Studio, llama.cpp's server, TGI, or a
    gateway in front of several of them. Many of those accept any non-empty
    key, so an unset key with a base URL configured is treated as usable.
    """

    name = "openai"

    def __init__(
        self, api_key: str | None = None, base_url: str | None = None
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._base_url = base_url or settings.openai_base_url
        self._client: Any = None

    @classmethod
    def available(cls) -> bool:
        return bool(settings.openai_api_key or settings.openai_base_url)

    def _get_client(self, timeout_seconds: float | None = None):
        if self._client is None:
            if not (self._api_key or self._base_url):
                raise BackendUnavailable(
                    "Set OPENAI_API_KEY or OPENAI_BASE_URL to use this backend"
                )

            try:
                import openai
            except ImportError as exc:  # pragma: no cover - packaging concern
                raise BackendUnavailable(
                    "The openai package is not installed"
                ) from exc

            if self._base_url and timeout_seconds is not None:
                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key or "not-needed",
                    base_url=self._base_url,
                    max_retries=0,
                    timeout=float(timeout_seconds),
                )
            elif self._base_url:
                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key or "not-needed",
                    base_url=self._base_url,
                    max_retries=0,
                )
            elif timeout_seconds is not None:
                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key or "not-needed",
                    max_retries=0,
                    timeout=float(timeout_seconds),
                )
            else:
                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key or "not-needed",
                    max_retries=0,
                )

        return self._client

    async def complete(self, request: CompletionRequest) -> str:
        client = self._get_client(request.timeout_seconds)

        message_kwargs = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }

        if request.temperature is not None:
            message_kwargs["temperature"] = request.temperature

        try:
            response = await client.chat.completions.create(**message_kwargs)
        except Exception as exc:
            import openai

            if isinstance(
                exc,
                (
                    openai.APIConnectionError,
                    openai.APITimeoutError,
                ),
            ):
                raise BackendTransientError(
                    f"OpenAI-compatible transient request failure: {exc}"
                ) from exc

            if isinstance(exc, openai.APIStatusError):
                if exc.status_code in {408, 409, 429} or exc.status_code >= 500:
                    raise BackendTransientError(
                        f"OpenAI-compatible transient HTTP "
                        f"{exc.status_code}: {exc}"
                    ) from exc

                raise BackendPermanentError(
                    f"OpenAI-compatible HTTP {exc.status_code}: {exc}"
                ) from exc

            raise BackendPermanentError(
                f"OpenAI-compatible request failed: {exc}"
            ) from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise BackendPermanentError(
                "OpenAI-compatible endpoint returned no choices"
            )

        message = getattr(choices[0], "message", None)
        text = getattr(message, "content", "") or ""

        if not text:
            raise BackendPermanentError(
                "OpenAI-compatible endpoint returned empty content"
            )

        return text

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None