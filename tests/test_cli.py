from __future__ import annotations

import httpx
from typer.testing import CliRunner

from polymarket_ai_agent.apps.operator.cli import app
from polymarket_ai_agent.types import DecisionStatus, ExecutionMode, ExecutionResult, SuggestedSide, TradeDecision

runner = CliRunner()


class StubService:
    def scan(self):
        raise NotImplementedError

    def discover_markets(self):
        class Market:
            market_id = "123"
            question = "Will BTC be up?"
            implied_probability = 0.52
            liquidity_usd = 2000.0

        return [Market()]

    def analyze_market(self, market_id):
        class Candidate:
            question = "Will BTC be up?"

        class Orderbook:
            midpoint = 0.52
            spread = 0.02

        class Snapshot:
            candidate = Candidate()
            orderbook = Orderbook()
            seconds_to_expiry = 100

        class Assessment:
            fair_probability = 0.58
            confidence = 0.8
            edge = 0.06
            suggested_side = SuggestedSide.YES
            reasons_for_trade = ["signal"]
            reasons_to_abstain = []

        return Snapshot(), Assessment()

    def paper_trade(self, market_id):
        snapshot, assessment = self.analyze_market(market_id)
        decision = TradeDecision(
            market_id=market_id,
            status=DecisionStatus.APPROVED,
            side=SuggestedSide.YES,
            size_usd=10.0,
            limit_price=0.52,
            rationale=["approved"],
            rejected_by=[],
        )
        result = ExecutionResult(
            market_id=market_id,
            success=True,
            mode=ExecutionMode.PAPER,
            order_id="paper-1",
            status="FILLED_PAPER",
            detail="ok",
        )
        return snapshot, assessment, decision, result

    def simulate_market(self, market_id):
        snapshot, assessment = self.analyze_market(market_id)
        decision = TradeDecision(
            market_id=market_id,
            status=DecisionStatus.APPROVED,
            side=SuggestedSide.YES,
            size_usd=10.0,
            limit_price=0.52,
            rationale=["simulated"],
            rejected_by=[],
        )
        return snapshot, assessment, decision

    def status(self):
        return {"trading_mode": "paper", "open_positions": 0}

    def auth_status(self):
        return {
            "live_client_constructible": False,
            "readonly_ready": False,
            "balance": None,
            "open_orders_count": 0,
        }

    def doctor(self, market_id=None):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "auth": {"readonly_ready": True, "balance": 44.93},
            "market": {"question": "Will BTC be up?", "seconds_to_expiry": 100},
            "orderbook": {"midpoint": 0.52, "spread": 0.02, "two_sided": True},
            "simulation": {"decision_status": "APPROVED", "decision_side": "YES"},
        }

    def live_preflight(self, market_id=None):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "ready": True,
            "blockers": [],
            "decision": {"status": "APPROVED", "asset_id": "token-yes"},
        }

    def live_orders(self):
        return {
            "readonly": True,
            "count": 1,
            "orders": [{"order_id": "live-1", "status": "OPEN"}],
        }

    def live_order_status(self, order_id):
        return {
            "readonly": True,
            "order": {"order_id": order_id, "status": "OPEN"},
        }

    def cancel_live_order(self, order_id):
        return {
            "readonly": False,
            "order": {"order_id": order_id, "status": "OPEN"},
            "cancellation": {"order_id": order_id, "success": True},
        }

    def live_trades(self, market_id=None, limit=20):
        return {
            "readonly": True,
            "count": 1,
            "trades": [{"trade_id": "trade-1", "order_id": "live-1"}],
        }

    def live_trade_status(self, trade_id, market_id=None, limit=100):
        return {
            "readonly": True,
            "trade": {"trade_id": trade_id, "order_id": "live-1"},
        }

    def live_activity(self, market_id=None, trade_limit=20):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "open_orders": {"count": 0, "orders": []},
            "recent_trades": {"count": 0, "trades": []},
            "preflight": {"ready": False, "blockers": ["edge_limit"]},
        }

    def tracked_live_orders(self, limit=50):
        return {
            "readonly": True,
            "count": 1,
            "orders": [{"order_id": "live-1", "status": "LIVE_SUBMITTED"}],
        }

    def refresh_live_order_tracking(self, limit=50):
        return {
            "readonly": True,
            "count": 1,
            "orders": [{"order_id": "live-1", "status": "MATCHED"}],
        }

    def live_reconcile(self, market_id=None, trade_limit=20, order_limit=50):
        return {
            "readonly": True,
            "market_id": market_id or "active-123",
            "tracked_orders": {"summary": {"active": 0, "terminal": 1, "errors": 0}},
            "recent_trades": {"count": 1, "trades": [{"trade_id": "trade-1"}]},
            "preflight": {"blockers": ["edge_limit"]},
        }

    def safety_stop_reason(self):
        return None

    def generate_operator_report(self, session_id=None):
        class Report:
            session_id = session_id or "session-1"
            items = ["item-1"]

        return Report()

    def manage_open_positions(self):
        class Action:
            market_id = "123"
            action = "CLOSE"
            reason = "ttl_expired"

        return [Action()]

    def close_position(self, market_id: str, reason: str = "manual_close"):
        return type(
            "Action",
            (),
            {
                "market_id": market_id,
                "action": "CLOSE",
                "reason": reason,
            },
        )()

    def run_cycle(self, market_id: str):
        return {
            "managed_actions": [],
            "paper_trade": {
                "market_id": market_id,
                "decision_status": "APPROVED",
                "decision_side": "YES",
                "execution_status": "FILLED_PAPER",
                "execution_success": True,
            },
        }

    def run_simulation_cycle(self, market_id: str):
        return {
            "market_id": market_id,
            "decision_status": "APPROVED",
            "decision_side": "YES",
            "limit_price": 0.52,
            "size_usd": 10.0,
            "rejected_by": [],
            "readonly": True,
        }

    def get_active_market_id(self):
        return "active-123"

    def live_trade(self, market_id):
        snapshot, assessment = self.analyze_market(market_id)
        decision = TradeDecision(
            market_id=market_id,
            status=DecisionStatus.APPROVED,
            side=SuggestedSide.YES,
            size_usd=10.0,
            limit_price=0.52,
            rationale=["approved"],
            rejected_by=[],
            asset_id="token-yes",
        )
        result = ExecutionResult(
            market_id=market_id,
            success=True,
            mode=ExecutionMode.LIVE,
            order_id="live-1",
            status="LIVE_SUBMITTED",
            detail="ok",
        )
        return snapshot, assessment, decision, result


def test_cli_status(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "trading_mode" in result.stdout


def test_cli_auth_check(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["auth-check"])
    assert result.exit_code == 0
    assert "readonly_ready" in result.stdout
    assert "open_orders_count" in result.stdout


def test_cli_doctor(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["doctor", "--active"])
    assert result.exit_code == 0
    assert "\"readonly\": true" in result.stdout
    assert "\"market_id\": \"active-123\"" in result.stdout
    assert "\"decision_status\": \"APPROVED\"" in result.stdout


def test_cli_live_preflight(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-preflight", "--active"])
    assert result.exit_code == 0
    assert "\"ready\": true" in result.stdout
    assert "\"asset_id\": \"token-yes\"" in result.stdout


def test_cli_live_orders(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-orders"])
    assert result.exit_code == 0
    assert "\"count\": 1" in result.stdout
    assert "\"order_id\": \"live-1\"" in result.stdout


def test_cli_live_order(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-order", "live-1"])
    assert result.exit_code == 0
    assert "\"order_id\": \"live-1\"" in result.stdout


def test_cli_live_cancel_requires_confirm(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-cancel", "live-1"])
    assert result.exit_code == 1
    assert "confirm-cancel" in result.stdout


def test_cli_live_cancel(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-cancel", "live-1", "--confirm-cancel"])
    assert result.exit_code == 0
    assert "\"success\": true" in result.stdout


def test_cli_live_trades(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-trades", "--limit", "5"])
    assert result.exit_code == 0
    assert "\"trade_id\": \"trade-1\"" in result.stdout


def test_cli_live_trade(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-trade", "trade-1"])
    assert result.exit_code == 0
    assert "\"trade_id\": \"trade-1\"" in result.stdout


def test_cli_live_activity(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-activity", "--active"])
    assert result.exit_code == 0
    assert "\"market_id\": \"active-123\"" in result.stdout
    assert "\"open_orders\"" in result.stdout
    assert "\"recent_trades\"" in result.stdout


def test_cli_tracked_live_orders(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["tracked-live-orders"])
    assert result.exit_code == 0
    assert "\"order_id\": \"live-1\"" in result.stdout


def test_cli_refresh_live_orders(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["refresh-live-orders"])
    assert result.exit_code == 0
    assert "\"status\": \"MATCHED\"" in result.stdout


def test_cli_live_reconcile(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-reconcile", "--active"])
    assert result.exit_code == 0
    assert "\"market_id\": \"active-123\"" in result.stdout
    assert "\"tracked_orders\"" in result.stdout
    assert "\"recent_trades\"" in result.stdout


def test_cli_live_watch(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live-watch", "--active", "--iterations", "2", "--interval-seconds", "0"])
    assert result.exit_code == 0
    assert "\"readonly\": true" in result.stdout
    assert "\"iterations_completed\": 2" in result.stdout
    assert "\"changed\": true" in result.stdout


def test_cli_live_requires_confirm(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live", "--active"])
    assert result.exit_code == 1
    assert "confirm-live" in result.stdout


def test_cli_live_with_confirm(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["live", "--active", "--confirm-live"])
    assert result.exit_code == 0
    assert "\"order_id\": \"live-1\"" in result.stdout
    assert "\"asset_id\": \"token-yes\"" in result.stdout


def test_cli_simulate(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["simulate", "123"])
    assert result.exit_code == 0
    assert "\"readonly\": true" in result.stdout
    assert "\"market_id\": \"123\"" in result.stdout


def test_cli_scan(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["scan", "--limit", "1"])
    assert result.exit_code == 0
    assert "Discovered Markets" in result.stdout


def test_cli_manage(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["manage"])
    assert result.exit_code == 0
    assert "ttl_expired" in result.stdout


def test_cli_close(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["close", "123", "--reason", "manual_close"])
    assert result.exit_code == 0
    assert "manual_close" in result.stdout


def test_cli_scan_handles_http_errors(monkeypatch) -> None:
    class FailingService(StubService):
        def discover_markets(self):
            raise httpx.ConnectError("dns failed")

    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: FailingService())
    result = runner.invoke(app, ["scan", "--limit", "1"])
    assert result.exit_code == 1
    assert "Request failed" in result.stdout


def test_cli_analyze_handles_runtime_errors(monkeypatch) -> None:
    class FailingService(StubService):
        def analyze_market(self, market_id):
            raise RuntimeError("market data unavailable")

    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: FailingService())
    result = runner.invoke(app, ["analyze", "123"])
    assert result.exit_code == 1
    assert "Operation failed" in result.stdout


def test_cli_run_loop(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["run-loop", "123", "--iterations", "2", "--interval-seconds", "0"])
    assert result.exit_code == 0
    assert "\"iterations_requested\": 2" in result.stdout
    assert "\"iterations_completed\": 2" in result.stdout


def test_cli_simulate_loop(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["simulate-loop", "123", "--iterations", "2", "--interval-seconds", "0"])
    assert result.exit_code == 0
    assert "\"readonly\": true" in result.stdout
    assert "\"iterations_completed\": 2" in result.stdout


def test_cli_paper_with_active_market(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["paper", "--active"])
    assert result.exit_code == 0
    assert "active-123" in result.stdout


def test_cli_simulate_with_active_market(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["simulate", "--active"])
    assert result.exit_code == 0
    assert "active-123" in result.stdout


def test_cli_run_loop_with_active_market(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["run-loop", "--active", "--iterations", "1", "--interval-seconds", "0"])
    assert result.exit_code == 0
    assert "active-123" in result.stdout


def test_cli_simulate_loop_with_active_market(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["simulate-loop", "--active", "--iterations", "1", "--interval-seconds", "0"])
    assert result.exit_code == 0
    assert "active-123" in result.stdout


def test_cli_run_loop_stops_early_on_safety_stop(monkeypatch) -> None:
    class SafetyStopService(StubService):
        def __init__(self):
            self.calls = 0

        def run_cycle(self, market_id: str):
            self.calls += 1
            return super().run_cycle(market_id)

        def safety_stop_reason(self):
            return "daily_loss_limit" if self.calls >= 1 else None

    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: SafetyStopService())
    result = runner.invoke(app, ["run-loop", "123", "--iterations", "3", "--interval-seconds", "0"])
    assert result.exit_code == 0
    assert "\"stopped_early\": true" in result.stdout
    assert "\"iterations_completed\": 1" in result.stdout
    assert "\"stop_reason\": \"daily_loss_limit\"" in result.stdout


def test_cli_paper_requires_market_or_active(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["paper"])
    assert result.exit_code == 1
    assert "Provide a market_id or pass --active." in result.stdout


def test_cli_simulate_requires_market_or_active(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["simulate"])
    assert result.exit_code == 1
    assert "Provide a market_id or pass --active." in result.stdout
