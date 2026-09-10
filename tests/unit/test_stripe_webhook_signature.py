# tests/unit/test_stripe_webhook_signature.py

import hashlib
import hmac
import time

from app.billing.stripe_webhooks import verify_stripe_signature
from app.utils.settings import settings

SECRET = "whsec_test_secret"


def sign(secret: str, payload: bytes, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode() + payload
    v1 = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={v1}"


def test_valid_signature_returns_true():
    payload = b'{"type": "checkout.session.completed"}'
    header = sign(SECRET, payload, int(time.time()))

    assert verify_stripe_signature(payload, header, SECRET) is True


def test_invalid_signature_returns_false():
    payload = b'{"type": "checkout.session.completed"}'
    header = sign("wrong-secret", payload, int(time.time()))

    assert verify_stripe_signature(payload, header, SECRET) is False


def test_expired_timestamp_returns_false():
    payload = b'{"type": "checkout.session.completed"}'
    old_timestamp = int(time.time()) - 301  # just past the 5-minute window
    header = sign(SECRET, payload, old_timestamp)

    assert verify_stripe_signature(payload, header, SECRET) is False


def test_missing_signature_header_returns_false():
    assert verify_stripe_signature(b"payload", "", SECRET) is False


def test_malformed_signature_header_returns_false():
    assert verify_stripe_signature(b"payload", "not-a-valid-header", SECRET) is False


def test_signature_header_missing_v1_returns_false():
    header = f"t={int(time.time())}"  # no v1 element present

    assert verify_stripe_signature(b"payload", header, SECRET) is False


def test_no_secret_configured_in_dev_bypasses_check(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")

    assert verify_stripe_signature(b"payload", "", "") is True


def test_no_secret_configured_in_production_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")

    assert verify_stripe_signature(b"payload", "", "") is False
