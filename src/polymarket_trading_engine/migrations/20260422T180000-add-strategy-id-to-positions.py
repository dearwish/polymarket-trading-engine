"""Add ``strategy_id`` to ``positions`` and ``order_attempts``.

Phase 1 of the adaptive-regime work: positions and order attempts become
keyed by (market_id, strategy_id) so multiple scorers can trade the same
market side-by-side with independent paper portfolios.

Existing rows are backfilled to ``'fade'`` — the name of the legacy
GBM-fade scorer that was the only strategy before this migration.
"""
from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)


def upgrade(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "positions", "strategy_id"):
        conn.execute(
            "ALTER TABLE positions ADD COLUMN strategy_id TEXT NOT NULL DEFAULT 'fade'"
        )
        conn.execute("UPDATE positions SET strategy_id = 'fade' WHERE strategy_id = ''")
    if not _has_column(conn, "order_attempts", "strategy_id"):
        conn.execute(
            "ALTER TABLE order_attempts ADD COLUMN strategy_id TEXT NOT NULL DEFAULT 'fade'"
        )
        conn.execute(
            "UPDATE order_attempts SET strategy_id = 'fade' WHERE strategy_id = ''"
        )
    # Composite index so per-strategy market lookups stay single-row.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS positions_strategy_market_status_idx "
        "ON positions(strategy_id, market_id, status)"
    )
