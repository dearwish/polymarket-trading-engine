"""Seed ``settings_changes`` with the code-defined baseline on first boot,
then ingest any legacy ``data/runtime_settings.json`` overrides.

Running again on a DB that already has settings rows would double-seed, so
the migration only acts when the table is empty. The migrations framework
records this file in ``migrations`` on success so it never runs twice
anyway, but the empty-table guard protects us against manual re-runs in
development.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from polymarket_ai_agent.initial_settings import INITIAL_SETTINGS_BASELINE


def upgrade(conn: sqlite3.Connection) -> None:
    # Empty-table guard. In normal operation this is always true on first run
    # and always false if someone re-executes the migration manually.
    row = conn.execute("SELECT COUNT(*) FROM settings_changes").fetchone()
    if row and int(row[0]) > 0:
        return

    now = datetime.now(timezone.utc).isoformat()

    # 1. Seed the curated baseline for every editable field.
    for field, value in INITIAL_SETTINGS_BASELINE.items():
        conn.execute(
            "INSERT INTO settings_changes(changed_at, field, value_before, value_after, source) "
            "VALUES (?, ?, NULL, ?, 'baseline')",
            (now, field, json.dumps(value)),
        )

    # 2. Ingest a legacy runtime_settings.json if one exists on disk. Any
    # value here overrides the baseline (newer rows win under the
    # "latest id per field" materialisation).
    legacy = Path("data/runtime_settings.json")
    if not legacy.exists():
        return
    try:
        overrides = json.loads(legacy.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Don't fail the migration on a malformed legacy file — just skip it.
        # Operator can hand-migrate the values through the dashboard.
        return
    if not isinstance(overrides, dict):
        return
    for field, value in overrides.items():
        if field not in INITIAL_SETTINGS_BASELINE:
            continue
        conn.execute(
            "INSERT INTO settings_changes(changed_at, field, value_before, value_after, source) "
            "VALUES (?, ?, NULL, ?, 'migration')",
            (now, field, json.dumps(value)),
        )
    # Drop the file so the DB is the single source of truth from here on.
    try:
        legacy.unlink()
    except OSError:
        pass
