from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import replace
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
    TERMINAL_LIVE_ORDER_STATUSES = {"CANCELED", "CANCELLED", "MATCHED", "FILLED", "EXECUTED", "REJECTED"}

    def __init__(
        self,
        db_path: Path,
        starting_balance_usd: float,
        exit_slippage_bps: float = 0.0,
        fee_bps: float = 0.0,
    ):
        self.db_path = db_path
        self.starting_balance_usd = starting_balance_usd
        # Applied at close: exit_price is reduced by exit_slippage_bps (the
        # sell-side slippage — we're closing the position regardless of side,
        # so both YES and NO exits get hit). fee_bps is deducted as a round-trip
        # cost from the realised PnL so paper accounting matches the scorer's
        # pre-trade edge calculation.
        self.exit_slippage_bps = max(0.0, float(exit_slippage_bps))
        self.fee_bps = max(0.0, float(fee_bps))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # --- maintenance --------------------------------------------------

    def prune_history(self, max_age_days: int, now: datetime | None = None) -> dict[str, int]:
        """Delete append-only history rows older than ``max_age_days``.

        Closed positions, rejected order attempts, and terminal live-order
        rows all accrue without bound on a long-running daemon. This helper
        is safe to run from a periodic maintenance task: open positions and
        active live orders are never touched.
        """
        if max_age_days <= 0:
            return {"order_attempts": 0, "positions": 0, "live_orders": 0}
        current = now or _utc_now()
        cutoff = (current - timedelta(days=max_age_days)).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            order_attempts = conn.execute(
                "delete from order_attempts where recorded_at < ?",
                (cutoff,),
            ).rowcount or 0
            positions = conn.execute(
                "delete from positions where status = 'CLOSED' and closed_at is not null and closed_at < ?",
                (cutoff,),
            ).rowcount or 0
            terminal = ",".join("?" for _ in self.TERMINAL_LIVE_ORDER_STATUSES)
            live_orders = conn.execute(
                f"delete from live_orders where status in ({terminal}) and updated_at < ?",
                (*self.TERMINAL_LIVE_ORDER_STATUSES, cutoff),
            ).rowcount or 0
            conn.commit()
        return {
            "order_attempts": int(order_attempts),
            "positions": int(positions),
            "live_orders": int(live_orders),
        }

    def vacuum(self) -> None:
        """Run a blocking VACUUM + WAL truncate.

        SQLite does not permit VACUUM inside a transaction and it takes an
        exclusive lock, so callers should schedule this from a maintenance
        task rather than the hot path. After the vacuum we truncate the WAL
        file so disk usage stays bounded.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.isolation_level = None  # autocommit — VACUUM can't run inside a txn
            conn.execute("vacuum")
            conn.execute("pragma wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def wal_checkpoint(self) -> tuple[int, int, int]:
        """Force a WAL checkpoint so the -wal sidecar cannot grow forever.

        Returns the raw (busy, log_pages, checkpointed_pages) tuple SQLite
        reports from ``pragma wal_checkpoint(TRUNCATE)`` so metrics can
        surface it.
        """
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
        if not row:
            return (0, 0, 0)
        return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))

    def backup(self, destination: Path) -> Path:
        """Write a consistent backup to ``destination`` using SQLite VACUUM INTO.

        VACUUM INTO is safe to call while the daemon is writing (with WAL
        enabled) and produces a standalone, compacted database file that can
        be rsync'd / uploaded off-host.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            # sqlite3 in Python 3.11+ does not allow parameterised VACUUM INTO,
            # so escape the path quote-quote style and keep the operator-supplied
            # destination separate from user-controlled strings.
            escaped = str(destination).replace("'", "''")
            conn.execute(f"vacuum into '{escaped}'")
        return destination

    def row_counts(self) -> dict[str, int]:
        """Cheap row counts for the `/api/metrics` gauges."""
        counts = {}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for table in ("positions", "order_attempts", "live_orders"):
                row = conn.execute(f"select count(*) from {table}").fetchone()
                counts[table] = int(row[0] or 0)
        return counts

    def get_account_state(self, mode: ExecutionMode, now: datetime | None = None) -> AccountState:
        open_positions = self.list_open_positions()
        realized_pnl = self.get_total_realized_pnl()
        daily_realized_pnl = self.get_daily_realized_pnl(now=now)
        rejected_orders = self.get_rejected_orders(now=now)
        reserved = sum(position.size_usd for position in open_positions)
        exposure = self._compute_exposure(open_positions)
        return AccountState(
            mode=mode,
            available_usd=self.starting_balance_usd + realized_pnl - reserved,
            open_positions=len(open_positions),
            daily_realized_pnl=daily_realized_pnl,
            rejected_orders=rejected_orders,
            long_btc_exposure_usd=exposure["long_btc_usd"],
            short_btc_exposure_usd=exposure["short_btc_usd"],
            net_btc_exposure_usd=exposure["net_btc_usd"],
            total_exposure_usd=exposure["total_exposure_usd"],
        )

    def get_exposure_summary(self) -> dict[str, float]:
        return self._compute_exposure(self.list_open_positions())

    @staticmethod
    def _compute_exposure(positions: list[PositionRecord]) -> dict[str, float]:
        """Approximate net BTC directional exposure across open positions.

        We treat YES on a "BTC up or down" market as a long-BTC bet and NO as a
        short-BTC bet. For threshold markets this is a first-order approximation
        (e.g. a YES on "BTC above $X" is still directionally long), good enough
        for a family-level correlation cap.
        """
        long_btc = 0.0
        short_btc = 0.0
        for position in positions:
            if position.side == SuggestedSide.YES:
                long_btc += position.size_usd
            elif position.side == SuggestedSide.NO:
                short_btc += position.size_usd
        return {
            "long_btc_usd": round(long_btc, 6),
            "short_btc_usd": round(short_btc, 6),
            "net_btc_usd": round(long_btc - short_btc, 6),
            "total_exposure_usd": round(long_btc + short_btc, 6),
        }

    def get_total_realized_pnl(self) -> float:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "select coalesce(sum(realized_pnl), 0.0) from positions where status = 'CLOSED'"
            ).fetchone()
        return float(row[0] or 0.0)

    def get_consecutive_losses(self, limit: int = 100) -> int:
        """Count CLOSED positions from the most recent backward until a
        non-losing close breaks the streak (realized_pnl > 0 → break).

        Losing = realized_pnl <= 0. A zero-PnL scratch still counts as a loss
        since it means the position did not earn anything to justify the risk.
        Limit bounds the scan; practical streaks that matter are < 20, so the
        default is generous. Returns 0 when no closed positions exist.
        """
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                select realized_pnl
                from positions
                where status = 'CLOSED' and closed_at is not null
                order by closed_at desc
                limit ?
                """,
                (int(max(1, limit)),),
            ).fetchall()
        streak = 0
        for (pnl,) in rows:
            if float(pnl or 0.0) > 0.0:
                break
            streak += 1
        return streak

    def get_daily_realized_pnl(self, now: datetime | None = None) -> float:
        current = now or _utc_now()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute(
                """
                select coalesce(sum(realized_pnl), 0.0)
                from positions
                where status = 'CLOSED'
                  and closed_at is not null
                  and substr(closed_at, 1, 10) = ?
                """,
                (current.date().isoformat(),),
            ).fetchone()
        return float(row[0] or 0.0)

    def record_execution(self, decision: TradeDecision, result: ExecutionResult) -> None:
        entry_price = result.fill_price if result.fill_price > 0 else decision.limit_price
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                insert into order_attempts(
                    market_id, success, counted_rejection, status, detail, recorded_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.market_id,
                    1 if result.success else 0,
                    1 if (not result.success and result.status != "SKIPPED") else 0,
                    result.status,
                    result.detail,
                    result.executed_at.isoformat(),
                ),
            )
            if result.mode == ExecutionMode.LIVE and result.order_id:
                conn.execute(
                    """
                    insert into live_orders(
                        order_id, market_id, asset_id, side, status, detail, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.order_id,
                        decision.market_id,
                        decision.asset_id,
                        decision.side.value,
                        result.status,
                        result.detail,
                        result.executed_at.isoformat(),
                        result.executed_at.isoformat(),
                    ),
                )
            if not result.success:
                conn.commit()
                return
            is_paper_fill = result.status == "FILLED_PAPER"
            is_live_fill = (
                result.mode == ExecutionMode.LIVE
                and result.fill_price > 0.0
                and result.filled_size_shares > 0.0
            )
            if not (is_paper_fill or is_live_fill):
                conn.commit()
                return
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

    def record_live_fill(
        self,
        order_id: str,
        market_id: str,
        asset_id: str,
        side: SuggestedSide,
        fill_price: float,
        filled_size_shares: float,
        filled_at: datetime | None = None,
    ) -> PositionRecord | None:
        """Create or update a PositionRecord for a live order that just filled.

        The user-channel reconciliation loop calls this when a Polymarket order
        transitions from ``MATCHED/FILLED`` — we persist the realised entry
        price and share size so paper and live positions follow an identical
        lifecycle (TTL exits, manage, close).
        """
        if fill_price <= 0.0 or filled_size_shares <= 0.0:
            return None
        timestamp = filled_at or _utc_now()
        size_usd = round(fill_price * filled_size_shares, 6)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            existing = conn.execute(
                "select market_id from positions where order_id = ? and status = 'OPEN' limit 1",
                (order_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    update positions
                    set entry_price = ?, size_usd = ?, opened_at = ?
                    where order_id = ? and status = 'OPEN'
                    """,
                    (fill_price, size_usd, timestamp.isoformat(), order_id),
                )
            else:
                conn.execute(
                    """
                    insert into positions(
                        market_id, side, size_usd, entry_price, order_id, opened_at, status,
                        close_reason, closed_at, exit_price, realized_pnl
                    ) values (?, ?, ?, ?, ?, ?, 'OPEN', '', NULL, 0.0, 0.0)
                    """,
                    (
                        market_id,
                        side.value,
                        size_usd,
                        fill_price,
                        order_id,
                        timestamp.isoformat(),
                    ),
                )
            conn.execute(
                """
                update live_orders
                set status = 'MATCHED',
                    detail = coalesce(detail, '') || ' | fill=' || ?,
                    updated_at = ?
                where order_id = ?
                """,
                (f"{fill_price:.6f}", timestamp.isoformat(), order_id),
            )
            conn.commit()
        return PositionRecord(
            market_id=market_id,
            side=side,
            size_usd=size_usd,
            entry_price=fill_price,
            order_id=order_id,
            opened_at=timestamp,
            status="OPEN",
        )

    def get_rejected_orders(self, now: datetime | None = None) -> int:
        current = now or _utc_now()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute(
                """
                select count(*)
                from order_attempts
                where counted_rejection = 1
                  and substr(recorded_at, 1, 10) = ?
                """,
                (current.date().isoformat(),),
            ).fetchone()
        return int(row[0] or 0)

    def list_live_orders(self, limit: int = 50) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                select order_id, market_id, asset_id, side, status, detail, created_at, updated_at
                from live_orders
                order by updated_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "order_id": str(row[0]),
                "market_id": str(row[1]),
                "asset_id": str(row[2]),
                "side": str(row[3]),
                "status": str(row[4]),
                "detail": str(row[5]),
                "created_at": str(row[6]),
                "updated_at": str(row[7]),
            }
            for row in rows
        ]

    def list_active_live_orders(self, limit: int = 50) -> list[dict]:
        return [order for order in self.list_live_orders(limit=limit) if not self.is_terminal_live_order_status(order["status"])]

    def list_terminal_live_orders(self, limit: int = 50) -> list[dict]:
        return [order for order in self.list_live_orders(limit=limit) if self.is_terminal_live_order_status(order["status"])]

    def update_live_order(self, order_id: str, status: str, detail: str = "", updated_at: datetime | None = None) -> None:
        current = updated_at or _utc_now()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                update live_orders
                set status = ?, detail = ?, updated_at = ?
                where order_id = ?
                """,
                (status, detail, current.isoformat(), order_id),
            )
            conn.commit()

    @classmethod
    def is_terminal_live_order_status(cls, status: str) -> bool:
        return status.strip().upper() in cls.TERMINAL_LIVE_ORDER_STATUSES

    def list_open_positions(self) -> list[PositionRecord]:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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

    def list_closed_tranches_for_order(self, base_order_id: str) -> list[PositionRecord]:
        """Return CLOSED tranche rows whose order_id starts with
        ``{base_order_id}-T``.

        Used on daemon startup to rehydrate the in-memory ladder state
        (tranches_closed + original_size_usd) for positions that were opened
        and partially closed in a previous daemon session.
        """
        if not base_order_id:
            return []
        like_pattern = f"{base_order_id}-T%"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                select market_id, side, size_usd, entry_price, order_id, opened_at, status,
                       close_reason, closed_at, exit_price, realized_pnl
                from positions
                where status = 'CLOSED' and order_id like ?
                order by closed_at asc
                """,
                (like_pattern,),
            ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def close_position(self, market_id: str, exit_price: float, reason: str, now: datetime | None = None) -> PositionAction:
        current = now or _utc_now()
        position = self._get_open_position(market_id)
        if not position:
            return PositionAction(market_id=market_id, action="NOOP", reason="Position not open.")
        pnl = self._compute_pnl(position, exit_price) - self._round_trip_fee(position.size_usd)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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

    def partial_close_position(
        self,
        market_id: str,
        fraction: float,
        exit_price: float,
        reason: str,
        now: datetime | None = None,
    ) -> PositionAction:
        """Close a fraction (0 < f < 1) of an open position.

        Splits the open row: the closed tranche becomes its own CLOSED row
        with a suffixed order_id; the remaining OPEN row's size_usd shrinks
        by the same fraction. fraction >= 1.0 falls back to a full close.
        """
        if fraction >= 1.0:
            return self.close_position(market_id, exit_price, reason, now=now)
        if fraction <= 0.0:
            return PositionAction(market_id=market_id, action="NOOP", reason="fraction must be > 0")
        current = now or _utc_now()
        position = self._get_open_position(market_id)
        if not position:
            return PositionAction(market_id=market_id, action="NOOP", reason="Position not open.")
        closed_size = position.size_usd * fraction
        remaining_size = position.size_usd - closed_size
        # Re-express the closed tranche as its own PositionRecord shape so we
        # reuse the same entry_price-based PnL formula.
        closed_tranche = replace(position, size_usd=closed_size)
        closed_pnl = self._compute_pnl(closed_tranche, exit_price) - self._round_trip_fee(closed_size)
        tranche_order_id = f"{position.order_id}-T{current.timestamp():.0f}" if position.order_id else ""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            # Shrink the open row.
            conn.execute(
                "update positions set size_usd = ? where market_id = ? and status = 'OPEN'",
                (remaining_size, market_id),
            )
            # Insert the closed tranche as a new row.
            conn.execute(
                """
                insert into positions(
                    market_id, side, size_usd, entry_price, order_id, opened_at, status,
                    close_reason, closed_at, exit_price, realized_pnl
                ) values (?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?)
                """,
                (
                    market_id,
                    position.side.value,
                    closed_size,
                    position.entry_price,
                    tranche_order_id,
                    position.opened_at.isoformat(),
                    reason,
                    current.isoformat(),
                    exit_price,
                    closed_pnl,
                ),
            )
            conn.commit()
        return PositionAction(market_id=market_id, action="PARTIAL_CLOSE", reason=reason)

    def _get_open_position(self, market_id: str) -> PositionRecord | None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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

    @staticmethod
    def estimate_exit_price(position: PositionRecord, orderbook, exit_slippage_bps: float) -> float:
        if position.side == SuggestedSide.YES:
            reference = orderbook.bid
        else:
            reference = max(0.01, 1 - orderbook.ask)
        slippage = reference * (exit_slippage_bps / 10_000)
        return round(max(0.01, min(0.99, reference - slippage)), 6)

    def max_paper_order_counter(self) -> int:
        """Return the largest N in any ``paper-order-NNNNNN`` order_id on
        record (positions or order_attempts). Used to seed the execution
        engine's counter after a restart so new trade IDs don't collide.
        Returns 0 when nothing relevant is stored yet.
        """
        max_n = 0
        pattern = re.compile(r"paper-order-(\d+)")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                select order_id from positions
                union all
                select order_id from live_orders
                """
            ).fetchall()
        for (order_id,) in rows:
            if not order_id:
                continue
            m = pattern.search(str(order_id))
            if m:
                try:
                    max_n = max(max_n, int(m.group(1)))
                except ValueError:
                    continue
        return max_n

    def apply_exit_slippage(self, exit_price: float) -> float:
        """Nudge a proposed exit mid by the configured slippage bps.

        Paper-mode callers should invoke this BEFORE close_position so the
        stored exit_price reflects realistic execution cost. Live-mode closes
        already receive a real CLOB fill price and must NOT double-apply this.
        Both YES and NO exits are sells so slippage always reduces the price.
        """
        if self.exit_slippage_bps <= 0.0 or exit_price <= 0.0:
            return exit_price
        adjusted = exit_price * (1.0 - self.exit_slippage_bps / 10_000.0)
        return round(max(0.01, min(0.99, adjusted)), 6)

    def _round_trip_fee(self, size_usd: float) -> float:
        """Round-trip fee on a position of ``size_usd`` at the configured bps.

        Applied at close (both full and partial), so buy+sell fee is recognised
        when the tranche closes. Units: dollars.
        """
        if self.fee_bps <= 0.0 or size_usd <= 0.0:
            return 0.0
        return float(size_usd) * (self.fee_bps / 10_000.0) * 2.0

    @staticmethod
    def _compute_pnl(position: PositionRecord, exit_price: float) -> float:
        # entry_price and exit_price are both stored in the token's own frame
        # (YES token price for YES positions, NO token price for NO positions).
        # So the PnL formula is uniform: (sell - buy) × shares.
        shares = position.size_usd / max(position.entry_price, 0.0001)
        return (exit_price - position.entry_price) * shares

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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            # WAL mode lets the daemon's async writes coexist with operator
            # reads (status/report/dashboard) without blocking. synchronous=NORMAL
            # is a good default for WAL journaling on a single-node trader.
            conn.execute("pragma journal_mode = WAL")
            conn.execute("pragma synchronous = NORMAL")
            conn.execute("pragma temp_store = MEMORY")
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
            conn.execute(
                """
                create table if not exists order_attempts (
                    market_id text not null,
                    success integer not null,
                    counted_rejection integer not null,
                    status text not null,
                    detail text not null,
                    recorded_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists live_orders (
                    order_id text primary key,
                    market_id text not null,
                    asset_id text not null,
                    side text not null,
                    status text not null,
                    detail text not null,
                    created_at text not null,
                    updated_at text not null
                )
                """
            )
            # Indexes keep hot-path reads O(log n) as the DB grows. All three
            # tables are append-heavy and queried by status/date or market_id.
            conn.execute("create index if not exists positions_status_idx on positions(status)")
            conn.execute("create index if not exists positions_market_status_idx on positions(market_id, status)")
            conn.execute("create index if not exists positions_closed_at_idx on positions(closed_at)")
            conn.execute("create index if not exists order_attempts_recorded_at_idx on order_attempts(recorded_at)")
            conn.execute("create index if not exists order_attempts_market_idx on order_attempts(market_id)")
            conn.execute("create index if not exists live_orders_status_idx on live_orders(status)")
            conn.execute("create index if not exists live_orders_updated_at_idx on live_orders(updated_at)")
            conn.commit()
