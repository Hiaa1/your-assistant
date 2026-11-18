from __future__ import annotations

from your_assistant.db import MIGRATIONS, connect, log_event, migrate


def test_migrate_is_idempotent(settings):
    connection = connect(settings.db_path)
    assert migrate(connection) == len(MIGRATIONS)
    assert migrate(connection) == len(MIGRATIONS)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    connection.close()


def test_connect_creates_parent_directory(settings):
    assert not settings.db_path.exists()
    connection = connect(settings.db_path)
    connection.close()
    assert settings.db_path.exists()


def test_log_event_persists_payload(conn):
    log_event(conn, "mail.sync", {"new": 2})
    row = conn.execute("SELECT kind, payload FROM events").fetchone()
    assert row["kind"] == "mail.sync"
    assert '"new": 2' in row["payload"]
