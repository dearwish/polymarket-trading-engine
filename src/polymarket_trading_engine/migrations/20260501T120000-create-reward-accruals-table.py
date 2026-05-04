"""Create ``reward_accruals`` table for paper-mode MM reward bookkeeping.

The market-maker strategy earns Polymarket's daily USDC subsidy when its
quotes rest in-band; the existing fill-based ``positions`` table can't
represent continuous accrual cleanly. This table is append-only — each
row is a "we accrued ``amount_usd`` over ``period_seconds`` ending at
``accrued_at`` for this (strategy, market, side) quote".

Aggregating with ``SUM(amount_usd)`` filtered by ``strategy_id`` gives
the strategy's running reward total, additive to the fill-based PnL on
``positions``.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reward_accruals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            side TEXT NOT NULL,
            accrued_at TEXT NOT NULL,
            period_seconds REAL NOT NULL,
            amount_usd REAL NOT NULL,
            in_band INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    # Strategy-id is the dominant lookup (per-strategy total, dashboard).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS reward_accruals_strategy_idx "
        "ON reward_accruals(strategy_id, accrued_at)"
    )
    # Date-bound queries on the running total ('today's reward income').
    conn.execute(
        "CREATE INDEX IF NOT EXISTS reward_accruals_accrued_at_idx "
        "ON reward_accruals(accrued_at)"
    )
