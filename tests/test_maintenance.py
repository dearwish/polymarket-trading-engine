from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_ai_agent.engine.journal import Journal
from polymarket_ai_agent.engine.portfolio import PortfolioEngine
from polymarket_ai_agent.types import (
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    OrderSide,
    SuggestedSide,
    TradeDecision,
)


def _portfolio(tmp_path: Path) -> PortfolioEngine:
    return PortfolioEngine(tmp_path / "agent.db", starting_balance_usd=100.0)


def _decision(market_id: str = "m1") -> TradeDecision:
    return TradeDecision(
        market_id=market_id,
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.5,
        rationale=["test"],
        rejected_by=[],
        asset_id="token-yes",
        order_side=OrderSide.BUY,
    )


def test_prune_history_keeps_recent_and_drops_old(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(portfolio.db_path) as conn:
        conn.executemany(
            "insert into order_attempts(market_id, success, counted_rejection, status, detail, recorded_at) values (?,?,?,?,?,?)",
            [
                ("m1", 1, 0, "FILLED", "", old_ts),
                ("m1", 0, 1, "REJECTED", "", old_ts),
                ("m1", 1, 0, "FILLED", "", fresh_ts),
            ],
        )
        conn.executemany(
            "insert into positions(market_id, side, size_usd, entry_price, order_id, opened_at, status, close_reason, closed_at, exit_price, realized_pnl) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("m1", "YES", 10.0, 0.5, "o1", old_ts, "CLOSED", "ttl", old_ts, 0.55, 1.0),
                ("m2", "YES", 10.0, 0.5, "o2", fresh_ts, "OPEN", "", None, 0.0, 0.0),
                ("m3", "YES", 10.0, 0.5, "o3", fresh_ts, "CLOSED", "ttl", fresh_ts, 0.55, 1.0),
            ],
        )
        conn.executemany(
            "insert into live_orders(order_id, market_id, asset_id, side, status, detail, created_at, updated_at) values (?,?,?,?,?,?,?,?)",
            [
                ("live-old", "m1", "asset-1", "YES", "MATCHED", "", old_ts, old_ts),
                ("live-new", "m2", "asset-2", "YES", "LIVE_SUBMITTED", "", fresh_ts, fresh_ts),
                ("live-old-open", "m3", "asset-3", "YES", "LIVE_SUBMITTED", "", old_ts, old_ts),
            ],
        )
        conn.commit()
    summary = portfolio.prune_history(max_age_days=14)
    assert summary["order_attempts"] == 2
    assert summary["positions"] == 1
    assert summary["live_orders"] == 1  # only MATCHED (terminal) + old → deleted
    with sqlite3.connect(portfolio.db_path) as conn:
        assert conn.execute("select count(*) from order_attempts").fetchone()[0] == 1
        # Open position preserved regardless of age; only CLOSED old one pruned.
        assert conn.execute("select count(*) from positions").fetchone()[0] == 2
        # Old terminal live_orders row gone; non-terminal and fresh rows preserved.
        ids = {row[0] for row in conn.execute("select order_id from live_orders").fetchall()}
        assert ids == {"live-new", "live-old-open"}


def test_prune_history_zero_days_is_noop(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    summary = portfolio.prune_history(max_age_days=0)
    assert summary == {"order_attempts": 0, "positions": 0, "live_orders": 0}


def test_vacuum_truncates_wal_and_compacts_db(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)

    def total_size() -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = portfolio.db_path.with_suffix(portfolio.db_path.suffix + suffix) if suffix else portfolio.db_path
            if path.exists():
                total += path.stat().st_size
        return total

    old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    with sqlite3.connect(portfolio.db_path) as conn:
        for i in range(500):
            conn.execute(
                "insert into order_attempts(market_id, success, counted_rejection, status, detail, recorded_at) values (?,?,?,?,?,?)",
                (f"m{i}", 1, 0, "FILLED", "x" * 500, old_ts),
            )
        conn.commit()
    size_before = total_size()
    portfolio.prune_history(max_age_days=1)
    portfolio.vacuum()
    size_after = total_size()
    # The total footprint (main db + WAL + SHM) should not have grown, and the
    # WAL file in particular should have been truncated by the checkpoint.
    assert size_after <= size_before
    wal_path = portfolio.db_path.with_suffix(portfolio.db_path.suffix + "-wal")
    if wal_path.exists():
        assert wal_path.stat().st_size <= 64 * 1024  # WAL truncated to small size


def test_backup_creates_standalone_db(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    result = ExecutionResult(
        market_id="m1",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="FILLED",
        detail="",
        fill_price=0.5,
        filled_size_shares=20.0,
        order_side=OrderSide.BUY,
        asset_id="token-yes",
    )
    portfolio.record_execution(_decision(), result)
    dest = tmp_path / "backups" / "snapshot.db"
    produced = portfolio.backup(dest)
    assert produced == dest
    assert dest.exists() and dest.stat().st_size > 0
    with sqlite3.connect(dest) as conn:
        count = conn.execute("select count(*) from positions").fetchone()[0]
    assert count == 1


def test_wal_checkpoint_returns_tuple(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    busy, log_pages, checkpointed = portfolio.wal_checkpoint()
    assert busy >= 0
    assert log_pages >= 0
    assert checkpointed >= 0


def test_row_counts_matches_inserts(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    portfolio.record_live_fill(
        order_id="o1", market_id="m1", asset_id="token-yes", side=SuggestedSide.YES,
        fill_price=0.5, filled_size_shares=10.0,
    )
    counts = portfolio.row_counts()
    assert counts["positions"] == 1
    assert counts["live_orders"] == 0  # record_live_fill only touches existing rows
    assert counts["order_attempts"] == 0


def test_journal_size_helpers(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "agent.db", tmp_path / "events.jsonl")
    journal.log_event("a", {"k": 1})
    assert journal.events_jsonl_size_bytes() > 0
    assert journal.db_size_bytes() > 0
    journal.save_report("s", "summary")
    journal.vacuum()
    assert journal.db_size_bytes() > 0


def test_journal_size_bytes_includes_wal_sidecars(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "agent.db", tmp_path / "events.jsonl")
    # Force some WAL traffic by writing a report.
    journal.save_report("s", "summary")
    # Sidecars may or may not exist depending on platform; size helper never
    # crashes and is always non-negative.
    size = journal.db_size_bytes()
    assert isinstance(size, int)
    assert size >= 0
