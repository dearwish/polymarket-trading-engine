from __future__ import annotations

from polymarket_ai_agent.engine.execution import ExecutionEngine
from polymarket_ai_agent.types import DecisionStatus, ExecutionMode, SuggestedSide, TradeDecision


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
    engine = ExecutionEngine(ExecutionMode.PAPER)
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
    assert result.success
    assert result.status == "FILLED_PAPER"


def test_execution_engine_blocks_live_trade_in_scaffold() -> None:
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
    assert result.status == "NOT_IMPLEMENTED"
