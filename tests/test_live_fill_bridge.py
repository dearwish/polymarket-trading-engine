from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from polymarket_trading_engine.engine.migrations import MigrationRunner
from polymarket_trading_engine.engine.portfolio import PortfolioEngine
from polymarket_trading_engine.types import (
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    OrderSide,
    SuggestedSide,
    TradeDecision,
)


def _portfolio(tmp_path: Path) -> PortfolioEngine:
    db_path = tmp_path / "agent.db"
    MigrationRunner(db_path).run()
    return PortfolioEngine(db_path=db_path, starting_balance_usd=100.0)


def test_record_execution_creates_position_for_live_fill(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    decision = TradeDecision(
        market_id="m1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["ok"],
        rejected_by=[],
        asset_id="token-yes",
        order_side=OrderSide.BUY,
    )
    result = ExecutionResult(
        market_id="m1",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-xyz",
        status="FILLED",
        detail="live fill",
        fill_price=0.50,
        filled_size_shares=20.0,
        order_side=OrderSide.BUY,
        asset_id="token-yes",
    )
    portfolio.record_execution(decision, result)
    positions = portfolio.list_open_positions()
    assert len(positions) == 1
    assert positions[0].side == SuggestedSide.YES
    assert positions[0].entry_price == 0.50
    assert positions[0].order_id == "live-xyz"


def test_record_execution_skips_position_for_live_submission_without_fill(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    decision = TradeDecision(
        market_id="m1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["ok"],
        rejected_by=[],
        asset_id="token-yes",
        order_side=OrderSide.BUY,
    )
    result = ExecutionResult(
        market_id="m1",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-abc",
        status="LIVE_SUBMITTED",
        detail="resting",
        fill_price=0.0,
        filled_size_shares=0.0,
        order_side=OrderSide.BUY,
        asset_id="token-yes",
    )
    portfolio.record_execution(decision, result)
    assert portfolio.list_open_positions() == []
    # Live order should still be tracked for reconciliation.
    tracked = portfolio.list_live_orders()
    assert len(tracked) == 1
    assert tracked[0]["order_id"] == "live-abc"


def test_record_live_fill_upgrades_resting_order_to_position(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    decision = TradeDecision(
        market_id="m1",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["ok"],
        rejected_by=[],
        asset_id="token-yes",
    )
    resting = ExecutionResult(
        market_id="m1",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-001",
        status="LIVE_SUBMITTED",
        detail="resting",
        fill_price=0.0,
    )
    portfolio.record_execution(decision, resting)
    record = portfolio.record_live_fill(
        order_id="live-001",
        market_id="m1",
        asset_id="token-yes",
        side=SuggestedSide.YES,
        fill_price=0.48,
        filled_size_shares=20.0,
        filled_at=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
    )
    assert record is not None
    positions = portfolio.list_open_positions()
    assert len(positions) == 1
    assert positions[0].entry_price == 0.48
    tracked = portfolio.list_live_orders()
    assert tracked[0]["status"] == "MATCHED"


def test_record_live_fill_ignores_zero_fills(tmp_path: Path) -> None:
    portfolio = _portfolio(tmp_path)
    record = portfolio.record_live_fill(
        order_id="x",
        market_id="m1",
        asset_id="token-yes",
        side=SuggestedSide.YES,
        fill_price=0.0,
        filled_size_shares=0.0,
    )
    assert record is None
    assert portfolio.list_open_positions() == []
