from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_ai_agent.types import (
    AccountState,
    ExecutionMode,
    ExecutionResult,
    PositionAction,
    PositionRecord,
    SuggestedSide,
    TradeDecision,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PortfolioEngine:
    def __init__(self, db_path: Path, starting_balance_usd: float):
        self.db_path = db_path
        self.starting_balance_usd = starting_balance_usd
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get_account_state(self, mode: ExecutionMode) -> AccountState:
        open_positions = self.list_open_positions()
        realized_pnl = self._get_realized_pnl()
        reserved = sum(position.size_usd for position in open_positions)
        return AccountState(
            mode=mode,
            available_usd=self.starting_balance_usd + realized_pnl - reserved,
            open_positions=len(open_positions),
            daily_realized_pnl=realized_pnl,
            rejected_orders=0,
        )

    def record_execution(self, decision: TradeDecision, result: ExecutionResult) -> None:
        if not result.success or result.status != "FILLED_PAPER":
            return
        entry_price = result.fill_price if result.fill_price > 0 else decision.limit_price
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                insert into positions(
                    market_id, side, size_usd, entry_price, order_id, opened_at, status,
                    close_reason, closed_at, exit_price, realized_pnl
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.market_id,
                    decision.side.value,
                    decision.size_usd,
                    entry_price,
                    result.order_id,
                    result.executed_at.isoformat(),
                    "OPEN",
                    "",
                    None,
                    0.0,
                    0.0,
                ),
            )
            conn.commit()

    def list_open_positions(self) -> list[PositionRecord]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                select market_id, side, size_usd, entry_price, order_id, opened_at, status,
                       close_reason, closed_at, exit_price, realized_pnl
                from positions where status = 'OPEN'
                order by opened_at asc
                """
            ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def list_closed_positions(self, limit: int = 20) -> list[PositionRecord]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                select market_id, side, size_usd, entry_price, order_id, opened_at, status,
                       close_reason, closed_at, exit_price, realized_pnl
                from positions where status = 'CLOSED'
                order by closed_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def positions_due_for_close(self, ttl_seconds: int, now: datetime | None = None) -> list[PositionRecord]:
        current = now or _utc_now()
        due: list[PositionRecord] = []
        for position in self.list_open_positions():
            if current - position.opened_at >= timedelta(seconds=ttl_seconds):
                due.append(position)
        return due

    def get_open_position(self, market_id: str) -> PositionRecord | None:
        return self._get_open_position(market_id)

    def close_position(self, market_id: str, exit_price: float, reason: str, now: datetime | None = None) -> PositionAction:
        current = now or _utc_now()
        position = self._get_open_position(market_id)
        if not position:
            return PositionAction(market_id=market_id, action="NOOP", reason="Position not open.")
        pnl = self._compute_pnl(position, exit_price)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                update positions
                set status = 'CLOSED',
                    close_reason = ?,
                    closed_at = ?,
                    exit_price = ?,
                    realized_pnl = ?
                where market_id = ? and status = 'OPEN'
                """,
                (reason, current.isoformat(), exit_price, pnl, market_id),
            )
            conn.commit()
        return PositionAction(market_id=market_id, action="CLOSE", reason=reason)

    def _get_open_position(self, market_id: str) -> PositionRecord | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                select market_id, side, size_usd, entry_price, order_id, opened_at, status,
                       close_reason, closed_at, exit_price, realized_pnl
                from positions where market_id = ? and status = 'OPEN'
                limit 1
                """,
                (market_id,),
            ).fetchone()
        return self._row_to_position(row) if row else None

    def _get_realized_pnl(self) -> float:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "select coalesce(sum(realized_pnl), 0.0) from positions where status = 'CLOSED'"
            ).fetchone()
        return float(row[0] or 0.0)

    @staticmethod
    def estimate_exit_price(position: PositionRecord, orderbook, exit_slippage_bps: float) -> float:
        if position.side == SuggestedSide.YES:
            reference = orderbook.bid
        else:
            reference = max(0.01, 1 - orderbook.ask)
        slippage = reference * (exit_slippage_bps / 10_000)
        return round(max(0.01, min(0.99, reference - slippage)), 6)

    @staticmethod
    def _compute_pnl(position: PositionRecord, exit_price: float) -> float:
        shares = position.size_usd / max(position.entry_price, 0.0001)
        if position.side == SuggestedSide.YES:
            return (exit_price - position.entry_price) * shares
        return ((1 - exit_price) - position.entry_price) * shares

    @staticmethod
    def _row_to_position(row) -> PositionRecord:
        closed_at = datetime.fromisoformat(row[8]) if row[8] else None
        return PositionRecord(
            market_id=str(row[0]),
            side=SuggestedSide(str(row[1])),
            size_usd=float(row[2]),
            entry_price=float(row[3]),
            order_id=str(row[4]),
            opened_at=datetime.fromisoformat(row[5]),
            status=str(row[6]),
            close_reason=str(row[7] or ""),
            closed_at=closed_at,
            exit_price=float(row[9] or 0.0),
            realized_pnl=float(row[10] or 0.0),
        )

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                create table if not exists positions (
                    market_id text not null,
                    side text not null,
                    size_usd real not null,
                    entry_price real not null,
                    order_id text not null,
                    opened_at text not null,
                    status text not null,
                    close_reason text,
                    closed_at text,
                    exit_price real,
                    realized_pnl real not null default 0.0
                )
                """
            )
            conn.commit()
