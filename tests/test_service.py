from __future__ import annotations

from polymarket_ai_agent.service import AgentService
from polymarket_ai_agent.types import (
    AccountState,
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    SuggestedSide,
    TradeDecision,
)


def test_agent_service_status(settings) -> None:
    service = AgentService(settings)
    status = service.status()
    assert status["trading_mode"] == settings.trading_mode
    assert status["market_family"] == settings.market_family
    assert status["live_trading_enabled"] == settings.live_trading_enabled
    assert "open_positions" in status
    assert "daily_realized_pnl" in status
    assert "rejected_orders" in status
    assert "daily_loss_limit_reached" in status
    assert "safety_stop_reason" in status
    assert "auth" in status
    assert "probe_attempted" in status["auth"]
    assert "readonly_ready" in status["auth"]


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
    service.execution.execute_trade = lambda decision, orderbook: ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="paper-order-1",
        status="FILLED_PAPER",
        detail="ok",
        fill_price=0.53,
    )
    service.portfolio.record_execution = lambda decision, result: recorded.__setitem__("called", True)
    _, _, decision, result = service.paper_trade("123")
    assert decision.status == DecisionStatus.APPROVED
    assert result.success
    assert recorded["called"]


def test_agent_service_simulate_market_is_readonly(settings, market_snapshot, market_assessment) -> None:
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
    called = {"record_execution": False}
    service.portfolio.record_execution = lambda decision, result: called.__setitem__("record_execution", True)
    _, _, decision = service.simulate_market("123")
    assert decision.status == DecisionStatus.APPROVED
    assert called["record_execution"] is False


def test_agent_service_generates_report(settings) -> None:
    service = AgentService(settings)
    report = service.generate_operator_report("session-abc")
    assert report.session_id == "session-abc"
    assert report.summary


def test_agent_service_manage_open_positions(settings, market_snapshot) -> None:
    service = AgentService(settings)

    class StubPosition:
        market_id = "123"
        side = SuggestedSide.YES

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
    service.portfolio.get_open_position = lambda market_id: type("Position", (), {"side": SuggestedSide.YES})()
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
    service.journal.log_event("execution_result", {"market_id": "open-1"})
    report = service.generate_operator_report("session-portfolio")
    assert "Open positions: 1" in report.summary
    assert any("EVENT |" in item for item in report.items)
    assert any("OPEN | open-1" in item for item in report.items)
    assert any("CLOSED | closed-1" in item for item in report.items)


def test_agent_service_run_cycle(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.manage_open_positions = lambda: []
    service.paper_trade = lambda market_id: (
        market_snapshot,
        market_assessment,
        type("Decision", (), {"status": type("Status", (), {"value": "APPROVED"})(), "side": type("Side", (), {"value": "YES"})()})(),
        type("Result", (), {"status": "FILLED_PAPER", "success": True, "fill_price": 0.53})(),
    )
    cycle = service.run_cycle("123")
    assert cycle["paper_trade"]["decision_status"] == "APPROVED"
    assert cycle["paper_trade"]["execution_status"] == "FILLED_PAPER"
    assert cycle["paper_trade"]["fill_price"] == 0.53


def test_agent_service_run_simulation_cycle(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.simulate_market = lambda market_id: (
        market_snapshot,
        market_assessment,
        type(
            "Decision",
            (),
            {
                "status": type("Status", (), {"value": "APPROVED"})(),
                "side": type("Side", (), {"value": "YES"})(),
                "limit_price": 0.52,
                "size_usd": 10.0,
                "rejected_by": [],
            },
        )(),
    )
    cycle = service.run_simulation_cycle("123")
    assert cycle["decision_status"] == "APPROVED"
    assert cycle["readonly"] is True
    assert cycle["fair_probability"] == market_assessment.fair_probability
    assert cycle["confidence"] == market_assessment.confidence
    assert cycle["suggested_side"] == market_assessment.suggested_side.value


def test_agent_service_report_formats_simulation_cycle_summary(settings) -> None:
    service = AgentService(settings)
    service.journal.log_event(
        "simulation_cycle",
        {
            "market_id": "1938163",
            "question": "Will the price of Bitcoin be above $82,000 on April 17?",
            "market_implied_probability": 0.002,
            "fair_probability": 0.0015,
            "confidence": 0.0,
            "edge": 0.0,
            "suggested_side": "ABSTAIN",
            "decision_status": "REJECTED",
            "decision_side": "ABSTAIN",
            "limit_price": 0.999,
            "size_usd": 0.0,
            "rejected_by": ["spread_limit", "confidence_limit", "edge_limit"],
            "readonly": True,
        },
    )
    report = service.generate_operator_report("session-sim")
    assert any("Will the price of Bitcoin be above $82,000 on April 17?" in item for item in report.items)
    assert any("decision=REJECTED" in item for item in report.items)
    assert any("rejected_by=spread_limit,confidence_limit,edge_limit" in item for item in report.items)


def test_agent_service_get_active_market_id(settings, market_candidate) -> None:
    service = AgentService(settings)
    service.polymarket.discover_active_market = lambda: market_candidate
    assert service.get_active_market_id() == market_candidate.market_id


def test_agent_service_safety_stop_reason(settings) -> None:
    service = AgentService(settings)
    account_state = AccountState(
        mode=ExecutionMode.PAPER,
        available_usd=100.0,
        open_positions=0,
        daily_realized_pnl=-settings.max_daily_loss_usd,
        rejected_orders=0,
    )
    assert service.safety_stop_reason(account_state) == "daily_loss_limit"


def test_agent_service_status_reports_auth_probe(settings) -> None:
    service = AgentService(settings)
    service.polymarket.probe_live_readiness = lambda: type(
        "Auth",
        (),
        {
            "private_key_configured": True,
            "funder_configured": False,
            "signature_type": 0,
            "live_client_constructible": True,
            "missing": [],
            "wallet_address": "0xabc",
            "api_credentials_derived": True,
            "server_ok": True,
            "readonly_ready": True,
            "probe_attempted": True,
            "collateral_address": "0x2791",
            "balance": 25.0,
            "allowance": 20.0,
            "open_orders_count": 2,
            "open_orders_markets": ["m1", "m2"],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    status = service.status()
    assert status["auth"]["wallet_address"] == "0xabc"
    assert status["auth"]["readonly_ready"] is True
    assert status["auth"]["balance"] == 25.0
    assert status["auth"]["open_orders_count"] == 2


def test_agent_service_auth_status(settings) -> None:
    service = AgentService(settings)
    service.polymarket.probe_live_readiness = lambda: type(
        "Auth",
        (),
        {
            "private_key_configured": True,
            "funder_configured": True,
            "signature_type": 1,
            "live_client_constructible": True,
            "missing": [],
            "wallet_address": "0xdef",
            "api_credentials_derived": True,
            "server_ok": True,
            "readonly_ready": True,
            "probe_attempted": True,
            "collateral_address": "0x2791",
            "balance": 10.0,
            "allowance": 9.0,
            "open_orders_count": 1,
            "open_orders_markets": ["m1"],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    auth = service.auth_status()
    assert auth["wallet_address"] == "0xdef"
    assert auth["readonly_ready"] is True
    assert auth["allowance"] == 9.0
    assert auth["open_orders_markets"] == ["m1"]


def test_agent_service_doctor(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.get_active_market_id = lambda: "123"
    service.polymarket.probe_live_readiness = lambda: type(
        "Auth",
        (),
        {
            "private_key_configured": True,
            "funder_configured": True,
            "signature_type": 2,
            "live_client_constructible": True,
            "missing": [],
            "wallet_address": "0xdef",
            "api_credentials_derived": True,
            "server_ok": True,
            "readonly_ready": True,
            "probe_attempted": True,
            "collateral_address": "0x2791",
            "balance": 44.93,
            "allowance": None,
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.simulate_market = lambda market_id: (
        market_snapshot,
        market_assessment,
        type(
            "Decision",
            (),
            {
                "status": type("Status", (), {"value": "APPROVED"})(),
                "side": type("Side", (), {"value": "YES"})(),
                "limit_price": 0.52,
                "size_usd": 10.0,
                "rejected_by": [],
            },
        )(),
    )
    report = service.doctor()
    assert report["readonly"] is True
    assert report["market_id"] == "123"
    assert report["auth"]["balance"] == 44.93
    assert report["market"]["question"] == market_snapshot.candidate.question
    assert report["simulation"]["decision_status"] == "APPROVED"


def test_agent_service_live_preflight_blocked(settings, market_snapshot, market_assessment) -> None:
    service = AgentService(settings)
    service.get_active_market_id = lambda: "123"
    service.polymarket.probe_live_readiness = lambda: type(
        "Auth",
        (),
        {
            "private_key_configured": True,
            "funder_configured": True,
            "signature_type": 2,
            "live_client_constructible": True,
            "missing": [],
            "wallet_address": "0xdef",
            "api_credentials_derived": True,
            "server_ok": True,
            "readonly_ready": True,
            "probe_attempted": True,
            "collateral_address": "0x2791",
            "balance": 44.93,
            "allowance": None,
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service._prepare_trade = lambda market_id, mode: (
        market_snapshot,
        market_assessment,
        TradeDecision(
            market_id="123",
            status=DecisionStatus.REJECTED,
            side=SuggestedSide.ABSTAIN,
            size_usd=0.0,
            limit_price=0.52,
            rationale=[],
            rejected_by=["edge_limit"],
        ),
        AccountState(
            mode=ExecutionMode.LIVE,
            available_usd=100.0,
            open_positions=0,
            daily_realized_pnl=0.0,
            rejected_orders=0,
        ),
    )
    preflight = service.live_preflight()
    assert preflight["ready"] is False
    assert "trading_mode_not_live" in preflight["blockers"]
    assert "live_trading_disabled" in preflight["blockers"]
    assert "edge_limit" in preflight["blockers"]


def test_agent_service_live_trade_executes_when_preflight_ready(settings, market_snapshot, market_assessment) -> None:
    configured = settings.model_copy(update={"trading_mode": "live", "live_trading_enabled": True})
    service = AgentService(configured)
    service.live_preflight = lambda market_id: {"blockers": []}
    service._prepare_trade = lambda market_id, mode: (
        market_snapshot,
        market_assessment,
        TradeDecision(
            market_id="123",
            status=DecisionStatus.APPROVED,
            side=SuggestedSide.YES,
            size_usd=10.0,
            limit_price=0.52,
            rationale=["approved"],
            rejected_by=[],
            asset_id="token-yes",
        ),
        AccountState(
            mode=ExecutionMode.LIVE,
            available_usd=100.0,
            open_positions=0,
            daily_realized_pnl=0.0,
            rejected_orders=0,
        ),
    )
    service.execution.execute_trade = lambda decision, orderbook: ExecutionResult(
        market_id="123",
        success=True,
        mode=ExecutionMode.LIVE,
        order_id="live-1",
        status="LIVE_SUBMITTED",
        detail="ok",
    )
    recorded = {"called": False}
    service.portfolio.record_execution = lambda decision, result: recorded.__setitem__("called", True)
    _, _, decision, result = service.live_trade("123")
    assert decision.asset_id == "token-yes"
    assert result.status == "LIVE_SUBMITTED"
    assert recorded["called"] is True


def test_agent_service_live_orders(settings) -> None:
    service = AgentService(settings)
    service.polymarket.probe_live_readiness = lambda: type(
        "Auth",
        (),
        {
            "private_key_configured": True,
            "funder_configured": True,
            "signature_type": 2,
            "live_client_constructible": True,
            "missing": [],
            "wallet_address": "0xdef",
            "api_credentials_derived": True,
            "server_ok": True,
            "readonly_ready": True,
            "probe_attempted": True,
            "collateral_address": "0x2791",
            "balance": 44.93,
            "allowance": None,
            "open_orders_count": 1,
            "open_orders_markets": ["m1"],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.polymarket.list_live_orders = lambda: [{"order_id": "live-1", "status": "OPEN"}]
    payload = service.live_orders()
    assert payload["count"] == 1
    assert payload["orders"][0]["order_id"] == "live-1"


def test_agent_service_live_order_status(settings) -> None:
    service = AgentService(settings)
    service.polymarket.probe_live_readiness = lambda: type(
        "Auth",
        (),
        {
            "private_key_configured": True,
            "funder_configured": True,
            "signature_type": 2,
            "live_client_constructible": True,
            "missing": [],
            "wallet_address": "0xdef",
            "api_credentials_derived": True,
            "server_ok": True,
            "readonly_ready": True,
            "probe_attempted": True,
            "collateral_address": "0x2791",
            "balance": 44.93,
            "allowance": None,
            "open_orders_count": 1,
            "open_orders_markets": ["m1"],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.polymarket.get_live_order = lambda order_id: {"order_id": order_id, "status": "OPEN"}
    payload = service.live_order_status("live-1")
    assert payload["order"]["order_id"] == "live-1"
