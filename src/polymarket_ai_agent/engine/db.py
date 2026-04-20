"""Shared SQLite connection helpers.

Engines used to run ``CREATE TABLE IF NOT EXISTS`` + pragma configuration in
their own ``_init_db()`` methods. Schema DDL has moved to the migrations
framework; this module owns the per-connection settings (WAL, synchronous,
temp_store) that every engine needs to apply whenever it opens a connection.
"""
from __future__ import annotations

import sqlite3


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pragmas the daemon relies on for concurrency + performance.

    - ``journal_mode=WAL`` lets the daemon's async writes coexist with operator
      reads (status, dashboard, report) without blocking.
    - ``synchronous=NORMAL`` is the durability/throughput sweet spot for WAL on
      a single-node trader.
    - ``temp_store=MEMORY`` keeps intermediate query state in RAM.

    ``journal_mode`` persists per-database; the others are per-connection, so
    call this on every newly opened ``sqlite3.Connection``.
    """
    conn.execute("pragma journal_mode = WAL")
    conn.execute("pragma synchronous = NORMAL")
    conn.execute("pragma temp_store = MEMORY")
