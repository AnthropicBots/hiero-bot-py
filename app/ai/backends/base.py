# app/ai/backends/base.py — The contract every review backend implements

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class BackendError(Exception):
    """Base class for errors raised while using an AI review backend."""


class BackendUnavailable(BackendError):
    """The backend is not configured or its required SDK is unavailable."""


class BackendTransientError(BackendError):
    """A temporary backend failure that may succeed if retried."""


class BackendPermanentError(BackendError):
    """A backend failure that should not be retried."""


@dataclass(frozen=True)
class CompletionRequest:
    """One review request, in terms every provider understands."""

    system: str
    prompt: str
    model: str
    max_tokens: int = 4096
    temperature: float | None = None
    timeout_seconds: int = 60


class ReviewBackend(ABC):
    """
    A source of model completions for AI review.

    Kept to a single method on purpose. The reviewer owns prompt construction
    and response parsing; a backend's only job is turning a
    `CompletionRequest` into raw text. That keeps a new provider — a local
    open-weight model, a self-hosted endpoint, or a gateway — to one small
    class with no knowledge of how reviews are shaped.
    """

    #: Identifier used in config (`ai_review.provider`) and in logs.
    name: str = ""

    @classmethod
    def available(cls) -> bool:
        """Whether this backend has everything it needs to run."""
        return False

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> str:
        """
        Return the model's raw text response.

        Implementations raise `BackendUnavailable` when the backend cannot be
        used because it is not configured or its required SDK is unavailable,
        `BackendTransientError` for failures that may succeed when retried,
        and `BackendPermanentError` for failures that should not be retried.
        """
        ...

    async def close(self) -> None:
        """Release held connections. Overridden by backends owning a client."""
        return