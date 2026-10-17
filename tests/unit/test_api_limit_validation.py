from pathlib import Path


def test_all_list_limits_require_positive():
    src = Path("app/api/routes.py").read_text()
    assert src.count("ge=1, le=") >= 4


def test_audit_limit_bounds():
    src = Path("app/api/routes.py").read_text()
    assert "Query(default=100, ge=1, le=500)" in src


def test_pr_health_limit_bounds():
    src = Path("app/api/routes.py").read_text()
    assert "Query(default=50, ge=1, le=200)" in src
