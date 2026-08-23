# tests/unit/test_rate_limit.py — token-bucket rate limiting (#19)

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.utils import ratelimit
from app.utils.ratelimit import Bucket, RateLimiter, RateLimitMiddleware, client_key
from app.utils.settings import settings

# ── Bucket mechanics ──────────────────────────────────────────


def test_first_request_is_allowed():
    limiter = RateLimiter(per_minute=60)
    allowed, remaining, retry_after = limiter.allow("ip", now=0.0)

    assert allowed
    assert remaining == 59
    assert retry_after == 0


def test_budget_is_exhausted_then_refused():
    limiter = RateLimiter(per_minute=3)

    results = [limiter.allow("ip", now=0.0)[0] for _ in range(4)]

    assert results == [True, True, True, False]


def test_refusal_reports_retry_after():
    limiter = RateLimiter(per_minute=60)
    for _ in range(60):
        limiter.allow("ip", now=0.0)

    allowed, _, retry_after = limiter.allow("ip", now=0.0)

    assert not allowed
    assert retry_after >= 1


def test_tokens_refill_over_time():
    limiter = RateLimiter(per_minute=60)  # one token per second
    for _ in range(60):
        limiter.allow("ip", now=0.0)

    assert limiter.allow("ip", now=0.5)[0] is False
    assert limiter.allow("ip", now=2.0)[0] is True


def test_refill_is_capped_at_burst():
    limiter = RateLimiter(per_minute=60, burst=10)
    limiter.allow("ip", now=0.0)

    # An hour of idling must not bank an hour's worth of tokens.
    allowed = [limiter.allow("ip", now=3600.0)[0] for _ in range(11)]

    assert allowed.count(True) == 10


def test_callers_have_independent_budgets():
    limiter = RateLimiter(per_minute=1)

    assert limiter.allow("a", now=0.0)[0] is True
    assert limiter.allow("b", now=0.0)[0] is True
    assert limiter.allow("a", now=0.0)[0] is False


def test_burst_defaults_to_the_minute_budget():
    assert RateLimiter(per_minute=42).burst == 42


def test_explicit_burst_overrides_default():
    assert RateLimiter(per_minute=60, burst=5).burst == 5


def test_bucket_retry_after_with_zero_rate():
    bucket = Bucket(tokens=0.0, updated_at=0.0)
    assert bucket.retry_after(rate=0, now=0.0) == 60


def test_idle_buckets_are_pruned(monkeypatch):
    monkeypatch.setattr(ratelimit, "_PRUNE_THRESHOLD", 5)
    limiter = RateLimiter(per_minute=60)

    for i in range(5):
        limiter.allow(f"old-{i}", now=0.0)

    limiter.allow("new", now=10_000.0)

    assert "old-0" not in limiter._buckets
    assert "new" in limiter._buckets


def test_reset_clears_state():
    limiter = RateLimiter(per_minute=1)
    limiter.allow("ip", now=0.0)
    limiter.reset()

    assert limiter.allow("ip", now=0.0)[0] is True


# ── Caller identification ─────────────────────────────────────



def make_request(
    host: str = "1.2.3.4",
    forwarded: str | None = None,
) -> Request:
    headers = {}

    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (key.encode(), value.encode())
            for key, value in headers.items()
        ],
        "client": (host, 12345),
        "scheme": "http",
        "server": ("testserver", 80),
    }

    async def receive():
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    return Request(scope, receive)


def test_forwarded_header_ignored_without_trusted_proxies(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)

    request = make_request(forwarded="9.9.9.9")

    assert client_key(request) == "1.2.3.4"


def test_forwarded_header_used_behind_one_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)

    request = make_request(
        forwarded="203.0.113.9, 10.0.0.1",
    )

    assert client_key(request) == "10.0.0.1"


def test_forwarded_chain_walked_back_by_hop_count(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)

    request = make_request(
        forwarded="203.0.113.9, 10.0.0.1, 10.0.0.2",
    )

    assert client_key(request) == "10.0.0.1"


def test_empty_forwarded_header_falls_back_to_peer(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)

    request = make_request(forwarded="  ")

    assert client_key(request) == "1.2.3.4"


def test_missing_client_is_labelled_unknown(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)

    request = make_request()
    request.scope["client"] = None

    assert client_key(request) == "unknown"


# ── Middleware ────────────────────────────────────────────────


def build_app(webhook_per_minute=2, api_per_minute=2):
    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/webhook", ok, methods=["POST"]),
            Route("/api/v1/health", ok),
            Route("/healthz", ok),
            Route("/", ok),
        ]
    )
    app.add_middleware(
        RateLimitMiddleware,
        webhook_limiter=RateLimiter(per_minute=webhook_per_minute),
        api_limiter=RateLimiter(per_minute=api_per_minute),
    )
    return app


@pytest.fixture
def limits_on(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)


@pytest.fixture
async def client(limits_on):
    async with AsyncClient(
        transport=ASGITransport(app=build_app()), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_api_requests_are_throttled(client):
    statuses = [(await client.get("/api/v1/health")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


@pytest.mark.asyncio
async def test_throttled_response_carries_retry_after(client):
    for _ in range(3):
        response = await client.get("/api/v1/health")

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert response.json()["detail"] == "Rate limit exceeded"


@pytest.mark.asyncio
async def test_allowed_responses_carry_budget_headers(client):
    response = await client.get("/api/v1/health")

    assert response.headers["X-RateLimit-Limit"] == "2"
    assert response.headers["X-RateLimit-Remaining"] == "1"


@pytest.mark.asyncio
async def test_webhook_and_api_budgets_are_separate(client):
    for _ in range(2):
        await client.get("/api/v1/health")

    api_response = await client.get("/api/v1/health")
    assert api_response.status_code == 429

    for _ in range(2):
        await client.post("/webhook")

    webhook_response = await client.post("/webhook")

    assert webhook_response.status_code == 429
    assert int(webhook_response.headers["Retry-After"]) >= 1
    assert webhook_response.headers["X-RateLimit-Remaining"] == "0"
    assert webhook_response.json()["detail"] == "Rate limit exceeded"


@pytest.mark.asyncio
async def test_healthz_is_never_throttled(client):
    statuses = [(await client.get("/healthz")).status_code for _ in range(10)]
    assert set(statuses) == {200}


@pytest.mark.asyncio
async def test_unlisted_paths_are_not_throttled(client):
    statuses = [(await client.get("/")).status_code for _ in range(10)]
    assert set(statuses) == {200}


@pytest.mark.asyncio
async def test_disabling_the_feature_removes_all_limits(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)

    async with AsyncClient(
        transport=ASGITransport(app=build_app()), base_url="http://test"
    ) as c:
        statuses = [(await c.get("/api/v1/health")).status_code for _ in range(5)]

    assert set(statuses) == {200}
