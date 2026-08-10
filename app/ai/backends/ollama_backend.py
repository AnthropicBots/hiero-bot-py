# app/ai/backends/ollama_backend.py

from __future__ import annotations

import httpx

from app.ai.backends.base import (
    BackendError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("ai.backend.ollama")


class OllamaBackend(ReviewBackend):
    """
    A local open-weight model served by Ollama.

    This is the backend that makes AI review usable on a private repository
    without sending source code anywhere: the endpoint is a process on your own
    machine or network. Talks Ollama's native `/api/chat` rather than its
    OpenAI shim, so it needs no SDK and no API key at all.
    """

    name = "ollama"

    def __init__(
        self, base_url: str | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base_url = (base_url or settings.ollama_base_url or "").rstrip("/")
        self._owns_client = client is None
        self._client = client

    @classmethod
    def available(cls) -> bool:
        return bool(settings.ollama_base_url)

    def _get_client(self, timeout_seconds: int) -> httpx.AsyncClient:
        if not self._base_url:
            raise BackendUnavailable("OLLAMA_BASE_URL is not set")

        if self._client is None:
            # Local models are slow; the timeout comes from the review config
            # rather than the 20s default used for GitHub calls.
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=float(timeout_seconds)
            )
        return self._client

    async def complete(self, request: CompletionRequest) -> str:
        client = self._get_client(request.timeout_seconds)

        payload = {
            "model": request.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }

        try:
            response = await client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise BackendError(f"Ollama request failed: {exc}") from exc

        if response.status_code == 404:
            raise BackendError(
                f"Ollama has no model named {request.model!r} — pull it first"
            )
        if response.status_code >= 400:
            raise BackendError(
                f"Ollama returned {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise BackendError("Ollama returned a non-JSON body") from exc

        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise BackendError("Ollama returned an empty message")
        return text

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
