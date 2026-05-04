from __future__ import annotations

from polymarket_trading_engine.service import AgentService
from polymarket_trading_engine.types import (
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
    assert "paper_available_usd" in status
    assert "funded_balance_usd" in status
    assert "available_usd_source" in status


def test_agent_service_status_prefers_funded_balance_when_auth_ready(settings) -> None:
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
            "wallet_address": "0xabc",
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
    status = service.status()
    assert status["available_usd"] == 44.93
    assert status["paper_available_usd"] == settings.paper_starting_balance_usd
    assert status["funded_balance_usd"] == 44.93
    assert status["available_usd_source"] == "funded_balance"


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
    service.execution.execute_trade = lambda decision, orderbook, **_: ExecutionResult(
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
        from polymarket_trading_engine.types import PositionAction

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
        from polymarket_trading_engine.types import PositionAction

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


def test_agent_service_safety_stop_does_not_halt_on_global_daily_loss(settings) -> None:
    """Daily-loss enforcement moved to RiskEngine (per-strategy) on 2026-04-29 —
    the daemon-wide safety_stop_reason no longer fires on aggregated PnL,
    so a single losing strategy can't freeze every other strategy.
    """
    service = AgentService(settings)
    account_state = AccountState(
        mode=ExecutionMode.PAPER,
        available_usd=100.0,
        open_positions=0,
        daily_realized_pnl=-settings.max_daily_loss_usd,
        rejected_orders=0,
    )
    assert service.safety_stop_reason(account_state) is None


def test_agent_service_safety_stop_still_fires_on_rejected_orders(settings) -> None:
    """Other daemon-wide kill-switch reasons (operational) still apply."""
    service = AgentService(settings)
    account_state = AccountState(
        mode=ExecutionMode.PAPER,
        available_usd=100.0,
        open_positions=0,
        daily_realized_pnl=0.0,
        rejected_orders=settings.max_rejected_orders,
    )
    assert service.safety_stop_reason(account_state) == "rejected_order_limit"


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
    service._prepare_trade = lambda market_id, mode, skip_scoring=False: (
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
    service.live_preflight = lambda market_id, skip_scoring=False: {"blockers": []}
    service._prepare_trade = lambda market_id, mode, skip_scoring=False: (
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
    service.execution.execute_trade = lambda decision, orderbook, **_: ExecutionResult(
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


def test_agent_service_cancel_live_order(settings) -> None:
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
    service.polymarket.cancel_live_order = lambda order_id: {"order_id": order_id, "success": True}
    payload = service.cancel_live_order("live-1")
    assert payload["order"]["order_id"] == "live-1"
    assert payload["cancellation"]["success"] is True


def test_agent_service_live_trades(settings) -> None:
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
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.polymarket.list_live_trades = lambda market_id=None, limit=20: [{"trade_id": "trade-1"}]
    payload = service.live_trades(limit=5)
    assert payload["count"] == 1
    assert payload["trades"][0]["trade_id"] == "trade-1"


def test_agent_service_live_trade_status(settings) -> None:
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
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.polymarket.get_live_trade = lambda trade_id, market_id=None, limit=100: {"trade_id": trade_id}
    payload = service.live_trade_status("trade-1")
    assert payload["trade"]["trade_id"] == "trade-1"


def test_agent_service_live_activity(settings) -> None:
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
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.live_preflight = lambda market_id=None, skip_scoring=False: {
        "market_id": market_id or "123",
        "ready": False,
        "blockers": ["edge_limit"],
        "market": {
            "condition_id": "cond-123",
            "seconds_to_expiry": 1800,
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
        },
    }
    service.polymarket.list_live_orders = lambda: [{"order_id": "live-1"}]
    service.polymarket.list_live_trades = lambda market_id=None, limit=20: [
        {"trade_id": "user-trade-1", "asset_id": "yes-token"},
    ]
    service.polymarket.list_market_trades = lambda market_id, limit=20: [
        {"trade_id": "trade-1", "asset_id": "yes-token", "outcome": "Yes"},
        {"trade_id": "trade-2", "asset_id": "no-token", "outcome": "No"},
        {"trade_id": "trade-3", "side": "NO", "outcome": "No"},
    ]
    service.portfolio.list_active_live_orders = lambda limit=50: [{"order_id": "live-1"}]
    service.portfolio.list_terminal_live_orders = lambda limit=50: [{"order_id": "live-2"}]
    payload = service.live_activity("123", trade_limit=5)
    assert payload["market_id"] == "123"
    assert payload["open_orders"]["count"] == 1
    assert payload["tracked_orders"]["active_count"] == 1
    assert payload["tracked_orders"]["terminal_count"] == 1
    assert payload["recent_trades"]["count"] == 1
    assert payload["preflight"]["blockers"] == ["edge_limit"]
    assert payload["last_poll"]["time_remaining_seconds"] == 1800
    assert payload["last_poll"]["market_trade_count"] == 3
    assert payload["last_poll"]["trade_counts"] == {"yes": 1, "no": 2, "other": 0, "total": 3}


def test_agent_service_tracked_live_orders(settings) -> None:
    service = AgentService(settings)
    service.portfolio.list_live_orders = lambda limit=50: [{"order_id": "live-1", "status": "LIVE_SUBMITTED"}] if limit == 5 else [{"order_id": "live-1", "status": "LIVE_SUBMITTED"}]
    payload = service.tracked_live_orders(limit=10)
    assert payload["count"] == 1
    assert payload["orders"][0]["order_id"] == "live-1"


def test_agent_service_refresh_live_order_tracking(settings) -> None:
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
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    updates = []
    service.portfolio.list_live_orders = lambda limit=50: [{"order_id": "live-1", "status": "LIVE_SUBMITTED"}]
    service.polymarket.get_live_order = lambda order_id: {"order_id": order_id, "status": "MATCHED"}
    service.portfolio.update_live_order = lambda order_id, status, detail="": updates.append((order_id, status))
    service.portfolio.is_terminal_live_order_status = lambda status: status == "MATCHED"
    payload = service.refresh_live_order_tracking(limit=5)
    assert payload["count"] == 1
    assert updates == [("live-1", "MATCHED")]
    assert payload["summary"] == {"active": 0, "terminal": 1, "errors": 0}


def test_agent_service_live_reconcile(settings) -> None:
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
            "open_orders_count": 0,
            "open_orders_markets": [],
            "diagnostics_collected": True,
            "errors": [],
        },
    )()
    service.live_preflight = lambda market_id=None, skip_scoring=False: {"market_id": market_id or "123", "ready": False, "blockers": ["edge_limit"]}
    service.refresh_live_order_tracking = lambda limit=50: {
        "readonly": True,
        "count": 1,
        "orders": [{"order_id": "live-1", "status": "MATCHED", "terminal": True}],
        "summary": {"active": 0, "terminal": 1, "errors": 0},
    }
    service.live_trades = lambda market_id=None, limit=20: {
        "readonly": True,
        "count": 1,
        "trades": [{"trade_id": "trade-1"}],
    }
    payload = service.live_reconcile("123", trade_limit=5, order_limit=7)
    assert payload["market_id"] == "123"
    assert payload["tracked_orders"]["summary"] == {"active": 0, "terminal": 1, "errors": 0}
    assert payload["recent_trades"]["count"] == 1
    assert payload["preflight"]["blockers"] == ["edge_limit"]


def test_live_preflight_skip_scoring_reuses_daemon_tick(settings) -> None:
    """With ``skip_scoring=True`` the preflight path must not call the
    scoring engine — it should reuse the most recent ``daemon_tick``
    payload for the active market. This is what the dashboard snapshot
    relies on to avoid OpenRouter round-trips on every poll.
    """
    from unittest.mock import MagicMock

    service = AgentService(settings)
    # Journal gets a fake daemon_tick for "active-123" so the lookup succeeds.
    service.journal.read_recent_events = lambda limit=2000: [  # type: ignore[assignment]
        {
            "event_type": "daemon_tick",
            "logged_at": "2026-04-23T06:00:00+00:00",
            "payload": {
                "market_id": "active-123",
                "suggested_side": "YES",
                "fair_probability": 0.62,
                "edge_yes": 0.05,
                "edge_no": -0.07,
                "confidence": 0.7,
            },
        }
    ]
    # Skip the snapshot-building path (requires market data on disk) by
    # short-circuiting _prepare_trade's surrounding helpers.
    service.build_market_snapshot = MagicMock(side_effect=RuntimeError("should not build"))  # type: ignore[assignment]
    service.analyze_market = MagicMock(side_effect=RuntimeError("should not analyze"))  # type: ignore[assignment]
    # We can't easily invoke the real live_preflight without the rest of the
    # snapshot/auth stack, so test the helper directly.
    assessment = service._latest_tick_assessment("active-123")
    assert assessment is not None
    assert assessment.suggested_side == SuggestedSide.YES
    assert assessment.fair_probability == 0.62
    assert assessment.edge == 0.05
    assert assessment.raw_model_output == "daemon-tick"
    # No matching tick → None (dashboard will fall back to the error shape).
    assert service._latest_tick_assessment("unknown-mkt") is None
