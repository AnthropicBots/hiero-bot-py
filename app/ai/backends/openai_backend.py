# app/ai/backends/openai_backend.py

from __future__ import annotations

from app.ai.backends.base import (
    BackendError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)
from app.utils.logger import get_logger
from app.utils.settings import settings

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
        self._client = None

    @classmethod
    def available(cls) -> bool:
        return bool(settings.openai_api_key or settings.openai_base_url)

    def _get_client(self):
        if self._client is None:
            if not (self._api_key or self._base_url):
                raise BackendUnavailable(
                    "Set OPENAI_API_KEY or OPENAI_BASE_URL to use this backend"
                )
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - packaging concern
                raise BackendUnavailable("The openai package is not installed") from exc

            self._client = openai.AsyncOpenAI(
                # Local servers ignore the key but the SDK requires one.
                api_key=self._api_key or "not-needed",
                base_url=self._base_url,
            )
        return self._client

    async def complete(self, request: CompletionRequest) -> str:
        client = self._get_client()

        try:
            response = await client.chat.completions.create(
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.prompt},
                ],
            )
        except Exception as exc:
            raise BackendError(f"OpenAI-compatible request failed: {exc}") from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise BackendError("OpenAI-compatible endpoint returned no choices")

        text = getattr(choices[0].message, "content", "") or ""
        if not text:
            raise BackendError("OpenAI-compatible endpoint returned empty content")
        return text
