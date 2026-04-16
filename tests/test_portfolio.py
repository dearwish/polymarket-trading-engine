from __future__ import annotations

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
