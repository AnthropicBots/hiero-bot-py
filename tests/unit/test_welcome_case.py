from pathlib import Path


def test_welcome_lookup_is_case_insensitive():
    src = Path("app/workflows/onboarding.py").read_text(encoding="utf-8")
    assert "func.lower(AuditLog.target_login)" in src
    assert "login.lower()" in src
