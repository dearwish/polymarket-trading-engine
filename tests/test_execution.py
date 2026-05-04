from __future__ import annotations

from polymarket_trading_engine.engine.execution import ExecutionEngine
from polymarket_trading_engine.types import DecisionStatus, ExecutionMode, ExecutionResult, SuggestedSide, TradeDecision


def test_execution_engine_skips_non_approved_trade() -> None:
    engine = ExecutionEngine(ExecutionMode.PAPER)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.ABSTAIN,
        side=SuggestedSide.ABSTAIN,
        size_usd=0.0,
        limit_price=0.0,
        rationale=["skip"],
        rejected_by=[],
    )
    result = engine.execute_trade(decision)
    assert not result.success
    assert result.status == "SKIPPED"


def test_execution_engine_executes_paper_trade() -> None:
    from polymarket_trading_engine.types import OrderBookSnapshot

    engine = ExecutionEngine(ExecutionMode.PAPER, paper_entry_slippage_bps=10)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["trade"],
        rejected_by=[],
    )
    orderbook = OrderBookSnapshot(
        bid=0.51,
        ask=0.52,
        midpoint=0.515,
        spread=0.01,
        depth_usd=500.0,
        last_trade_price=0.515,
    )
    result = engine.execute_trade(decision, orderbook)
    assert result.success
    assert result.status == "FILLED_PAPER"
    assert result.fill_price > 0.52


def test_execution_engine_fills_no_side_in_no_token_frame() -> None:
    """NO-side BUY should fill at the NO token price (1 - YES bid), not YES ask.

    Before the fix, NO-side trades walked YES asks and stored entry_price in the
    YES frame, which then inverted PnL in Portfolio._compute_pnl.
    """
    from polymarket_trading_engine.types import OrderBookSnapshot

    engine = ExecutionEngine(ExecutionMode.PAPER, paper_entry_slippage_bps=0)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.NO,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["trade"],
        rejected_by=[],
    )
    # Asymmetric YES book so YES ask ≠ NO ask and the test discriminates the fix.
    # YES bid=0.30, ask=0.40 → NO ask = 1 - 0.30 = 0.70, NO bid = 1 - 0.40 = 0.60.
    # A NO buyer pays 0.70 per NO share.
    orderbook = OrderBookSnapshot(
        bid=0.30,
        ask=0.40,
        midpoint=0.35,
        spread=0.10,
        depth_usd=500.0,
        last_trade_price=0.35,
        bid_levels=[(0.30, 100.0), (0.29, 200.0)],
        ask_levels=[(0.40, 100.0), (0.41, 200.0)],
    )
    result = engine.execute_trade(decision, orderbook)
    assert result.success
    assert result.status == "FILLED_PAPER"
    # Fill price must be in NO frame: best NO ask = 1 - 0.30 = 0.70.
    # Before the fix this was 0.40 (YES ask) — wrong token frame → inverted PnL.
    assert abs(result.fill_price - 0.70) < 0.001
    assert "of NO @" in result.detail


def test_execution_engine_blocks_live_trade_when_disabled() -> None:
    engine = ExecutionEngine(ExecutionMode.LIVE)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["trade"],
        rejected_by=[],
    )
    result = engine.execute_trade(decision)
    assert not result.success
    assert result.status == "LIVE_DISABLED"


def test_execution_engine_requires_asset_id_for_live_trade() -> None:
    engine = ExecutionEngine(ExecutionMode.LIVE, live_trading_enabled=True)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["trade"],
        rejected_by=[],
    )
    result = engine.execute_trade(decision)
    assert not result.success
    assert result.status == "LIVE_INVALID"


def test_execution_engine_uses_live_executor_when_enabled() -> None:
    def live_executor(decision, orderbook):
        return ExecutionResult(
            market_id=decision.market_id,
            success=True,
            mode=ExecutionMode.LIVE,
            order_id="live-1",
            status="LIVE_SUBMITTED",
            detail="submitted",
        )

    engine = ExecutionEngine(ExecutionMode.LIVE, live_trading_enabled=True, live_executor=live_executor)
    decision = TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["trade"],
        rejected_by=[],
        asset_id="token-yes",
    )
    result = engine.execute_trade(decision)
    assert result.success
    assert result.status == "LIVE_SUBMITTED"
