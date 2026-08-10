# app/ai/backends/base.py — The contract every review backend implements

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class BackendError(Exception):
    """The backend was reachable but could not produce a completion."""


class BackendUnavailable(BackendError):
    """The backend is not configured — no key, no endpoint, or no SDK installed."""


@dataclass(frozen=True)
class CompletionRequest:
    """One review request, in terms every provider understands."""

    system: str
    prompt: str
    model: str
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout_seconds: int = 60


class ReviewBackend(ABC):
    """
    A source of model completions for AI review.

    Kept to a single method on purpose. The reviewer owns prompt construction
    and response parsing; a backend's only job is turning a
    `CompletionRequest` into raw text. That keeps a new provider — a local
    open-weight model, a gateway, a self-hosted endpoint — to one small class
    with no knowledge of how reviews are shaped.
    """

    #: Identifier used in config (`ai_review.provider`) and in logs.
    name: str = ""

    @classmethod
    def available(cls) -> bool:
        """Whether this backend has everything it needs to run."""
        return False

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> str:
        """Return the model's raw text response, or raise `BackendError`."""

    async def close(self) -> None:
        """Release held connections. Overridden by backends owning a client."""
        return
