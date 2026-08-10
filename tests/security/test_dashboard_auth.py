# tests/security/test_dashboard_auth.py — dashboard / API access control (#20)

import base64

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.utils.auth import _auth_configured, _matches, require_dashboard_auth
from app.utils.settings import settings

USER = "maintainer"
PASSWORD = "correct-horse-battery-staple"


def build_app():
    app = FastAPI()

    @app.get("/protected")
    async def protected(_: None = Depends(require_dashboard_auth)):
        return {"ok": True}

    return app


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=build_app()), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def credentials_set(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_username", USER)
    monkeypatch.setattr(settings, "dashboard_password", PASSWORD)


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ── Enforcement ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correct_credentials_are_accepted(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, PASSWORD))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_credentials_are_rejected(client, credentials_set):
    response = await client.get("/protected")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


@pytest.mark.asyncio
async def test_wrong_password_is_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, "guess"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_username_is_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic("intruder", PASSWORD))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_empty_credentials_are_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic("", ""))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_password_prefix_is_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, PASSWORD[:-1]))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_authorization_header_is_rejected(client, credentials_set):
    response = await client.get(
        "/protected", headers={"Authorization": "Basic not-base64"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bearer_token_is_not_accepted(client, credentials_set):
    response = await client.get(
        "/protected", headers={"Authorization": f"Bearer {PASSWORD}"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_username_is_rejected_not_crashed(client, credentials_set):
    response = await client.get("/protected", headers=basic("maintaıner", PASSWORD))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_password_is_rejected_not_crashed(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, "pässwörd"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_credentials_are_unusable_end_to_end(client, monkeypatch):
    """
    Documents where the ASCII boundary actually is: FastAPI's HTTPBasic decodes
    the header as ASCII and refuses anything else, so a non-ASCII dashboard
    password can never be supplied — it locks the operator out rather than
    reaching our comparison. Worth knowing before choosing one.
    """
    monkeypatch.setattr(settings, "dashboard_username", "wächter")
    monkeypatch.setattr(settings, "dashboard_password", "pässwörd")

    response = await client.get("/protected", headers=basic("wächter", "pässwörd"))
    assert response.status_code == 401


# ── Configuration ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unset_credentials_leave_the_endpoint_open(client, monkeypatch):
    """Documented, deliberate, and called out in SECURITY.md — verified here."""
    monkeypatch.setattr(settings, "dashboard_username", None)
    monkeypatch.setattr(settings, "dashboard_password", None)

    response = await client.get("/protected")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_password_without_username_does_not_half_enable_auth(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "dashboard_username", None)
    monkeypatch.setattr(settings, "dashboard_password", PASSWORD)

    assert _auth_configured() is False
    assert (await client.get("/protected")).status_code == 200


@pytest.mark.asyncio
async def test_username_without_password_does_not_half_enable_auth(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "dashboard_username", USER)
    monkeypatch.setattr(settings, "dashboard_password", None)

    assert _auth_configured() is False
    assert (await client.get("/protected")).status_code == 200


@pytest.mark.asyncio
async def test_empty_string_credentials_count_as_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")

    assert _auth_configured() is False


# ── Comparison primitive ──────────────────────────────────────


def test_matches_is_exact():
    assert _matches("abc", "abc") is True
    assert _matches("abc", "abd") is False
    assert _matches("abc", "abcd") is False
    assert _matches("", "") is True


def test_matches_handles_non_ascii_without_raising():
    assert _matches("üser", "user") is False
    assert _matches("üser", "üser") is True


def test_dependency_raises_http_exception_not_a_bare_error(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_username", USER)
    monkeypatch.setattr(settings, "dashboard_password", PASSWORD)

    import asyncio

    with pytest.raises(HTTPException):
        asyncio.run(require_dashboard_auth(credentials=None))
