from unittest.mock import AsyncMock

import pytest

from app.github.webhooks import WebhookRouter


@pytest.mark.asyncio
async def test_installation_skips_when_account_missing():
    router = WebhookRouter.__new__(WebhookRouter)
    payload = {"action": "created", "installation": {"id": 1, "account": {}}}
    db = AsyncMock()
    result = await WebhookRouter._handle_installation(router, payload, db)
    assert result["skipped"] == "missing account info"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_installation_skips_without_id():
    router = WebhookRouter.__new__(WebhookRouter)
    db = AsyncMock()
    result = await WebhookRouter._handle_installation(router, {"action": "created"}, db)
    assert result["skipped"] == "no installation id"
