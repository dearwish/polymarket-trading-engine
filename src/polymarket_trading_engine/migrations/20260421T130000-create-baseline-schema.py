"""Adopt the legacy schema that used to be created inline by
``PortfolioEngine._init_db()`` and ``Journal._init_db()``. Idempotent on
existing databases; full schema bootstrap on a fresh one.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    # Core trading tables (previously in PortfolioEngine._init_db).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            market_id    TEXT NOT NULL,
            side         TEXT NOT NULL,
            size_usd     REAL NOT NULL,
            entry_price  REAL NOT NULL,
            order_id     TEXT NOT NULL,
            opened_at    TEXT NOT NULL,
            status       TEXT NOT NULL,
            close_reason TEXT,
            closed_at    TEXT,
            exit_price   REAL,
            realized_pnl REAL NOT NULL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_attempts (
            market_id         TEXT NOT NULL,
            success           INTEGER NOT NULL,
            counted_rejection INTEGER NOT NULL,
            status            TEXT NOT NULL,
            detail            TEXT NOT NULL,
            recorded_at       TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_orders (
            order_id   TEXT PRIMARY KEY,
            market_id  TEXT NOT NULL,
            asset_id   TEXT NOT NULL,
            side       TEXT NOT NULL,
            status     TEXT NOT NULL,
            detail     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Journal reports table (previously in Journal._init_db).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            session_id TEXT NOT NULL,
            summary    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Indexes — identical to the inline ones, so existing DBs stay no-ops.
    conn.execute("CREATE INDEX IF NOT EXISTS positions_status_idx ON positions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS positions_market_status_idx ON positions(market_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS positions_closed_at_idx ON positions(closed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS order_attempts_recorded_at_idx ON order_attempts(recorded_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS order_attempts_market_idx ON order_attempts(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS live_orders_status_idx ON live_orders(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS live_orders_updated_at_idx ON live_orders(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS reports_created_at_idx ON reports(created_at)")
