# app/ai/backends/ollama_backend.py

from __future__ import annotations

import httpx

from app.ai.backends.base import (
    BackendPermanentError,
    BackendTransientError,
    BackendUnavailable,
    CompletionRequest,
    ReviewBackend,
)
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("ai.backend.ollama")


class OllamaBackend(ReviewBackend):
    """
    A model served by an Ollama-compatible endpoint.

    The endpoint may be local or hosted on trusted infrastructure. Talks to
    Ollama's native `/api/chat` endpoint rather than its OpenAI shim, so it
    needs no Ollama SDK or API key.
    """

    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or settings.ollama_base_url or "").rstrip("/")
        self._owns_client = client is None
        self._client = client

    @classmethod
    def available(cls) -> bool:
        return bool(settings.ollama_base_url)

    def _get_client(self, timeout_seconds: float) -> httpx.AsyncClient:
        if not self._base_url:
            raise BackendUnavailable("OLLAMA_BASE_URL is not set")

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=float(timeout_seconds),
            )

        return self._client

    async def complete(self, request: CompletionRequest) -> str:
        client = self._get_client(request.timeout_seconds)

        options: dict[str, int | float] = {
            "num_predict": request.max_tokens,
        }

        if request.temperature is not None:
            options["temperature"] = request.temperature

        payload = {
            "model": request.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "options": options,
        }

        try:
            response = await client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise BackendTransientError(
                f"Ollama request timed out: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            raise BackendTransientError(
                f"Ollama transport failure: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendPermanentError(
                f"Ollama request failed: {exc}"
            ) from exc

        if response.status_code in {408, 409, 429} or response.status_code >= 500:
            raise BackendTransientError(
                f"Ollama returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        if response.status_code >= 400:
            raise BackendPermanentError(
                f"Ollama returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise BackendPermanentError(
                "Ollama returned a non-JSON body"
            ) from exc

        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise BackendPermanentError("Ollama returned an empty message")

        return text

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None