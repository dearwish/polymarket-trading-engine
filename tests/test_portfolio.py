from __future__ import annotations

import sqlite3
from datetime import timedelta

from polymarket_ai_agent.engine.portfolio import PortfolioEngine
from polymarket_ai_agent.types import DecisionStatus, ExecutionMode, ExecutionResult, SuggestedSide, TradeDecision, utc_now


def test_portfolio_records_paper_execution(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.53,
    )
    engine.record_execution(decision, result)
    positions = engine.list_open_positions()
    assert len(positions) == 1
    assert positions[0].market_id == "123"
    assert positions[0].entry_price == 0.53
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.open_positions == 1
    assert account_state.available_usd == settings.paper_starting_balance_usd - 10.0


def test_portfolio_marks_due_positions(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.52,
    )
    engine.record_execution(decision, result)
    due = engine.positions_due_for_close(60, now=utc_now() + timedelta(seconds=61))
    assert len(due) == 1


def test_portfolio_closes_position_and_realizes_pnl(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["approved"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.50,
    )
    engine.record_execution(decision, result)
    action = engine.close_position("123", exit_price=0.60, reason="ttl_expired")
    assert action.action == "CLOSE"
    assert engine.list_open_positions() == []
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.daily_realized_pnl > 0
    closed = engine.list_closed_positions(limit=5)
    assert len(closed) == 1
    assert closed[0].close_reason == "ttl_expired"


def test_portfolio_estimates_exit_price_for_yes(settings) -> None:
    from polymarket_ai_agent.types import OrderBookSnapshot, PositionRecord

    orderbook = OrderBookSnapshot(
        bid=0.60,
        ask=0.62,
        midpoint=0.61,
        spread=0.02,
        depth_usd=100.0,
        last_trade_price=0.61,
    )
    position = PositionRecord(market_id="123", side=SuggestedSide.YES, size_usd=10.0, entry_price=0.50)
    price = PortfolioEngine.estimate_exit_price(position, orderbook, exit_slippage_bps=10)
    assert price < 0.60


def test_portfolio_daily_realized_pnl_ignores_previous_days(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    opened_at = utc_now() - timedelta(days=1, minutes=5)
    closed_at = utc_now() - timedelta(days=1)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            """
            insert into positions(
                market_id, side, size_usd, entry_price, order_id, opened_at, status,
                close_reason, closed_at, exit_price, realized_pnl
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-1",
                SuggestedSide.YES.value,
                10.0,
                0.50,
                "paper-old",
                opened_at.isoformat(),
                "CLOSED",
                "ttl_expired",
                closed_at.isoformat(),
                0.40,
                -2.0,
            ),
        )
        conn.commit()
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.daily_realized_pnl == 0.0
    assert engine.get_total_realized_pnl() == -2.0


def test_portfolio_counts_rejected_orders_without_counting_skips(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    failed_result = ExecutionResult(
        market_id="123",
        success=False,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="NOT_IMPLEMENTED",
        detail="disabled",
    )
    skipped_result = ExecutionResult(
        market_id="124",
        success=False,
        mode=ExecutionMode.PAPER,
        order_id="paper-2",
        status="SKIPPED",
        detail="not approved",
    )
    engine.record_execution(decision, failed_result)
    engine.record_execution(decision, skipped_result)
    account_state = engine.get_account_state(ExecutionMode.PAPER)
    assert account_state.rejected_orders == 1


def test_portfolio_tracks_live_order_submission(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="LIVE_SUBMITTED",
        detail="submitted",
    )
    engine.record_execution(decision, result)
    tracked = engine.list_live_orders()
    assert len(tracked) == 1
    assert tracked[0]["order_id"] == "live-1"
    assert tracked[0]["status"] == "LIVE_SUBMITTED"


def test_portfolio_updates_tracked_live_order(settings) -> None:
    engine = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="LIVE_SUBMITTED",
        detail="submitted",
    )
    engine.record_execution(decision, result)
    engine.update_live_order("live-1", status="MATCHED", detail="filled")
    tracked = engine.list_live_orders()
    assert tracked[0]["status"] == "MATCHED"
    assert tracked[0]["detail"] == "filled"
