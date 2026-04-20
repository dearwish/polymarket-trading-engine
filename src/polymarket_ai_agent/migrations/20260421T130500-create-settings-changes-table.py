"""Create the settings_changes audit log table.

Every operator override lands here as an append-only row with a before/after
delta, source, and timestamp. Current effective value of each field =
``MAX(id)``-indexed latest row per ``field``.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings_changes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            changed_at   TEXT NOT NULL,
            field        TEXT NOT NULL,
            value_before TEXT,
            value_after  TEXT NOT NULL,
            source       TEXT NOT NULL,
            actor        TEXT,
            reason       TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS settings_changes_field_idx "
        "ON settings_changes(field)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS settings_changes_changed_at_idx "
        "ON settings_changes(changed_at)"
    )
