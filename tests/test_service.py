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
    assert "open_positions" in status
    assert "auth" in status


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
    recorded = {"called": False}
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
    service.portfolio.record_execution = lambda decision, result: recorded.__setitem__("called", True)
    _, _, decision, result = service.paper_trade("123")
    assert decision.status == DecisionStatus.APPROVED
    assert result.success
    assert recorded["called"]


def test_agent_service_generates_report(settings) -> None:
    service = AgentService(settings)
    report = service.generate_operator_report("session-abc")
    assert report.session_id == "session-abc"
    assert report.summary


def test_agent_service_manage_open_positions(settings, market_snapshot) -> None:
    service = AgentService(settings)

    class StubPosition:
        market_id = "123"

    service.portfolio.positions_due_for_close = lambda ttl_seconds: [StubPosition()]
    service.build_market_snapshot = lambda market_id: market_snapshot
    actions_seen = []

    def close_position(market_id: str, exit_price: float, reason: str):
        from polymarket_ai_agent.types import PositionAction

        action = PositionAction(market_id=market_id, action="CLOSE", reason=reason)
        actions_seen.append((market_id, exit_price, reason))
        return action

    service.portfolio.close_position = close_position
    actions = service.manage_open_positions()
    assert len(actions) == 1
    assert actions[0].action == "CLOSE"
    assert actions_seen[0][2] == "ttl_expired"


def test_agent_service_close_position(settings, market_snapshot) -> None:
    service = AgentService(settings)
    service.portfolio.get_open_position = lambda market_id: object()
    service.build_market_snapshot = lambda market_id: market_snapshot
    seen = {}

    def close_position(market_id: str, exit_price: float, reason: str):
        from polymarket_ai_agent.types import PositionAction

        seen["call"] = (market_id, exit_price, reason)
        return PositionAction(market_id=market_id, action="CLOSE", reason=reason)

    service.portfolio.close_position = close_position
    action = service.close_position("123", reason="manual_close")
    assert action.action == "CLOSE"
    assert seen["call"][0] == "123"
    assert seen["call"][2] == "manual_close"


def test_agent_service_close_position_noop_when_missing(settings) -> None:
    service = AgentService(settings)
    service.portfolio.get_open_position = lambda market_id: None
    action = service.close_position("missing", reason="manual_close")
    assert action.action == "NOOP"
    assert action.reason == "Position not open."


def test_agent_service_report_includes_portfolio_summaries(settings) -> None:
    service = AgentService(settings)

    class OpenPosition:
        market_id = "open-1"
        side = type("Side", (), {"value": "YES"})()
        size_usd = 10.0
        entry_price = 0.52

    class ClosedPosition:
        market_id = "closed-1"
        realized_pnl = 1.25
        close_reason = "manual_close"

    service.portfolio.list_open_positions = lambda: [OpenPosition()]
    service.portfolio.list_closed_positions = lambda limit=5: [ClosedPosition()]
    report = service.generate_operator_report("session-portfolio")
    assert "Open positions: 1" in report.summary
    assert any("OPEN | open-1" in item for item in report.items)
    assert any("CLOSED | closed-1" in item for item in report.items)


def test_agent_service_run_cycle(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.manage_open_positions = lambda: []
    service.paper_trade = lambda market_id: (
        market_snapshot,
        market_assessment,
        type("Decision", (), {"status": type("Status", (), {"value": "APPROVED"})(), "side": type("Side", (), {"value": "YES"})()})(),
        type("Result", (), {"status": "FILLED_PAPER", "success": True})(),
    )
    cycle = service.run_cycle("123")
    assert cycle["paper_trade"]["decision_status"] == "APPROVED"
    assert cycle["paper_trade"]["execution_status"] == "FILLED_PAPER"
