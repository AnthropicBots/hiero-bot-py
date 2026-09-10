# tests/unit/test_database.py

from unittest.mock import MagicMock, patch

from app.db.database import _set_sqlite_pragma, engine


def test_pragma_runs_on_sqlite():
    dbapi_connection = MagicMock()
    with patch.object(engine.dialect, "name", "sqlite"):
        _set_sqlite_pragma(dbapi_connection, None)

    cursor = dbapi_connection.cursor.return_value
    cursor.execute.assert_any_call("PRAGMA journal_mode=WAL")
    cursor.execute.assert_any_call("PRAGMA busy_timeout=30000")
    cursor.close.assert_called_once()


def test_pragma_skipped_on_postgres():
    dbapi_connection = MagicMock()
    with patch.object(engine.dialect, "name", "postgresql"):
        _set_sqlite_pragma(dbapi_connection, None)

    dbapi_connection.cursor.assert_not_called()
