from __future__ import annotations

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

    def status(self):
        return {"trading_mode": "paper"}

    def generate_operator_report(self, session_id=None):
        class Report:
            session_id = session_id or "session-1"
            items = ["item-1"]

        return Report()


def test_cli_status(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "trading_mode" in result.stdout


def test_cli_scan(monkeypatch) -> None:
    monkeypatch.setattr("polymarket_ai_agent.apps.operator.cli._service", lambda: StubService())
    result = runner.invoke(app, ["scan", "--limit", "1"])
    assert result.exit_code == 0
    assert "Discovered Markets" in result.stdout
