from __future__ import annotations

from polymarket_ai_agent.service import AgentService
from polymarket_ai_agent.types import (
    AccountState,
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    TradeDecision,
)


def test_agent_service_status(settings) -> None:
    service = AgentService(settings)
    status = service.status()
    assert status["trading_mode"] == settings.trading_mode
    assert status["market_family"] == settings.market_family


def test_agent_service_discover_markets_logs(settings, market_candidate) -> None:
    service = AgentService(settings)
    service.polymarket.discover_markets = lambda: [market_candidate]
    markets = service.discover_markets()
    assert len(markets) == 1
    assert settings.events_path.exists()


def test_agent_service_analyze_market(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.build_market_snapshot = lambda market_id: market_snapshot
    service.scoring.score_market = lambda packet: market_assessment
    snapshot, assessment = service.analyze_market("123")
    assert snapshot.candidate.market_id == "123"
    assert assessment.market_id == "123"


def test_agent_service_paper_trade(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.analyze_market = lambda market_id: (market_snapshot, market_assessment)
    service.risk.decide_trade = lambda snapshot, assessment, account_state: TradeDecision(
        market_id="123",
        status=DecisionStatus.APPROVED,
        side=assessment.suggested_side,
        size_usd=10.0,
        limit_price=0.52,
        rationale=["approved"],
        rejected_by=[],
    )
    service.execution.execute_trade = lambda decision: ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-order-1",
        status="FILLED_PAPER",
        detail="ok",
    )
    _, _, decision, result = service.paper_trade("123")
    assert decision.status == DecisionStatus.APPROVED
    assert result.success


def test_agent_service_generates_report(settings) -> None:
    service = AgentService(settings)
    report = service.generate_operator_report("session-abc")
    assert report.session_id == "session-abc"
    assert report.summary
