from pathlib import Path


def test_fk_pragma_enabled():
    src = Path("app/db/database.py").read_text()
    assert "PRAGMA foreign_keys=ON" in src
