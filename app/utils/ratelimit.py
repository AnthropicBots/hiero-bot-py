# app/utils/ratelimit.py — Token-bucket rate limiting for public endpoints

from __future__ import annotations

import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("utils.ratelimit")

# Paths that must never be throttled: a load balancer polling health should not
# be able to lock itself out, and it carries no cost worth protecting.
EXEMPT_PATHS = frozenset({"/healthz"})

# Buckets are pruned once the table grows past this, so a spray of requests from
# many source addresses cannot grow the process's memory without limit.
_PRUNE_THRESHOLD = 10_000


@dataclass
class Bucket:
    """A token bucket: `tokens` refill at `rate` per second, capped at `burst`."""

    tokens: float
    updated_at: float

    def take(self, *, rate: float, burst: float, now: float) -> bool:
        elapsed = now - self.updated_at
        self.tokens = min(burst, self.tokens + elapsed * rate)
        self.updated_at = now

        if self.tokens < 1.0:
            return False

        self.tokens -= 1.0
        return True

    def retry_after(self, *, rate: float, now: float) -> int:
        """Whole seconds until at least one token is available again."""
        if rate <= 0:
            return 60
        missing = max(0.0, 1.0 - self.tokens)
        return max(1, int(missing / rate) + 1)


class RateLimiter:
    """
    Fixed-rate token buckets keyed by caller identity.

    State lives in this process only. Behind multiple workers each one enforces
    the configured rate independently, so the effective cluster limit is
    `workers × limit`; a shared limiter belongs at the reverse proxy. This is
    here to stop a single client hammering one instance, not to meter a fleet.
    """

    def __init__(self, *, per_minute: int, burst: int | None = None) -> None:
        self.per_minute = per_minute
        self.rate = per_minute / 60.0
        self.burst = float(burst if burst is not None else per_minute)
        self._buckets: dict[str, Bucket] = {}

    def allow(self, key: str, *, now: float | None = None) -> tuple[bool, int, int]:
        """
        Consume one token for `key`.

        Returns (allowed, remaining, retry_after_seconds).
        """
        now = time.monotonic() if now is None else now

        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= _PRUNE_THRESHOLD:
                self._prune(now)
            bucket = Bucket(tokens=self.burst, updated_at=now)
            self._buckets[key] = bucket

        allowed = bucket.take(rate=self.rate, burst=self.burst, now=now)
        remaining = int(bucket.tokens)
        retry_after = 0 if allowed else bucket.retry_after(rate=self.rate, now=now)
        return allowed, remaining, retry_after

    def _prune(self, now: float) -> None:
        """Drop buckets that have refilled completely — they carry no state."""
        full_after = self.burst / self.rate if self.rate > 0 else 0
        stale = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.updated_at > full_after
        ]
        for key in stale:
            del self._buckets[key]
        log.debug("Pruned %d idle rate-limit buckets", len(stale))

    def reset(self) -> None:
        self._buckets.clear()


def client_key(request: Request) -> str:
    """
    Identify the caller.

    `X-Forwarded-For` is only consulted when `trusted_proxy_hops` says a proxy
    is in front of us; otherwise any client could set the header and hand
    itself a fresh bucket per request.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("X-Forwarded-For", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if chain:
            # The right-most entries are added by our own proxies; step back
            # past them to reach the address the outermost proxy observed.
            index = max(0, len(chain) - hops)
            return chain[index]

    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies a per-caller request budget to the webhook and API surfaces."""

    def __init__(
        self,
        app,
        *,
        webhook_limiter: RateLimiter | None = None,
        api_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self.webhook = webhook_limiter or RateLimiter(
            per_minute=settings.rate_limit_webhook_per_minute,
            burst=settings.rate_limit_burst or None,
        )
        self.api = api_limiter or RateLimiter(
            per_minute=settings.rate_limit_api_per_minute,
            burst=settings.rate_limit_burst or None,
        )

    def _limiter_for(self, path: str) -> tuple[RateLimiter | None, str]:
        if path in EXEMPT_PATHS:
            return None, ""
        if path == "/webhook":
            return self.webhook, "webhook"
        if path.startswith("/api/"):
            return self.api, "api"
        return None, ""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.rate_limit_enabled:
            return await call_next(request)

        limiter, scope = self._limiter_for(request.url.path)
        if limiter is None:
            return await call_next(request)

        key = f"{scope}:{client_key(request)}"
        allowed, remaining, retry_after = limiter.allow(key)

        if not allowed:
            log.warning(
                "Rate limit exceeded on %s by %s", request.url.path, key
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limiter.per_minute),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limiter.per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
