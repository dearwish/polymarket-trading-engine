"""Append-only settings-change store backed by the ``settings_changes`` table.

Single source of truth for runtime overrides: current effective value of a
field is the ``value_after`` of the most recently inserted row for that
field. Schema is owned by the migrations framework (never created here).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_ai_agent.engine.db import configure_connection


@dataclass(slots=True, frozen=True)
class SettingsChangeRow:
    id: int
    changed_at: str
    field: str
    value_before: Any
    value_after: Any
    source: str
    actor: str | None = None
    reason: str | None = None


class SettingsStore:
    """Thin CRUD over ``settings_changes``.

    Callers:
      * daemon reload loop — ``get_max_id`` + ``list_changes(since_id)``.
      * API / CLI writers — ``record_changes``.
      * ``get_effective_settings`` — ``current_overrides`` materialises
        "latest row per field" on every read, which is cheap thanks to
        the ``settings_changes_field_idx`` index.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    # --- materialisation -----------------------------------------------

    def current_overrides(self) -> dict[str, Any]:
        """Return ``{field: value}`` for every field that has at least one row.

        Query takes the max ``id`` per field so later inserts always win —
        matches the append-only "latest wins" semantics.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT field, value_after
                FROM settings_changes
                WHERE id IN (SELECT MAX(id) FROM settings_changes GROUP BY field)
                """
            ).fetchall()
        return {str(field): _decode(value_after) for field, value_after in rows}

    # --- reads for the daemon reload loop ------------------------------

    def get_max_id(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM settings_changes").fetchone()
        return int(row[0] or 0)

    def list_changes(self, since_id: int = 0) -> list[SettingsChangeRow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, changed_at, field, value_before, value_after, source, actor, reason "
                "FROM settings_changes WHERE id > ? ORDER BY id ASC",
                (int(since_id),),
            ).fetchall()
        return [_row_to_change(r) for r in rows]

    def list_timeline(self) -> list[SettingsChangeRow]:
        return self.list_changes(since_id=0)

    # --- write ---------------------------------------------------------

    def record_changes(
        self,
        changes: list[tuple[str, Any, Any]],
        source: str,
        actor: str | None = None,
        reason: str | None = None,
    ) -> list[int]:
        """Insert one row per ``(field, before, after)`` triple in a single
        transaction. Returns the inserted row IDs in order.

        Callers compute the diff against the current effective state before
        calling — the store doesn't second-guess what "before" means.
        """
        if not changes:
            return []
        now = datetime.now(timezone.utc).isoformat()
        ids: list[int] = []
        with closing(self._connect()) as conn, conn:
            for field, value_before, value_after in changes:
                cursor = conn.execute(
                    "INSERT INTO settings_changes"
                    "(changed_at, field, value_before, value_after, source, actor, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        field,
                        None if value_before is None else json.dumps(value_before),
                        json.dumps(value_after),
                        source,
                        actor,
                        reason,
                    ),
                )
                ids.append(int(cursor.lastrowid))
        return ids

    # --- internals -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        configure_connection(conn)
        return conn


def _decode(raw: Any) -> Any:
    """Decode a JSON-encoded ``value_after`` / ``value_before`` column.

    Legacy rows that pre-date the JSON convention might be bare strings;
    fall back to the raw value if JSON decoding fails so bad data doesn't
    crash the daemon.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def _row_to_change(row: tuple) -> SettingsChangeRow:
    return SettingsChangeRow(
        id=int(row[0]),
        changed_at=str(row[1]),
        field=str(row[2]),
        value_before=_decode(row[3]),
        value_after=_decode(row[4]),
        source=str(row[5]),
        actor=str(row[6]) if row[6] is not None else None,
        reason=str(row[7]) if row[7] is not None else None,
    )
