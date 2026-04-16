from __future__ import annotations

import uuid

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.connectors.external_feeds import ExternalFeedConnector
from polymarket_ai_agent.connectors.polymarket import PolymarketConnector
from polymarket_ai_agent.engine.execution import ExecutionEngine
from polymarket_ai_agent.engine.journal import Journal
from polymarket_ai_agent.engine.research import ResearchEngine
from polymarket_ai_agent.engine.risk import RiskEngine
from polymarket_ai_agent.engine.scoring import ScoringEngine
from polymarket_ai_agent.types import (
    AccountState,
    ExecutionMode,
    MarketAssessment,
    MarketSnapshot,
    OrderBookSnapshot,
    Report,
    SuggestedSide,
    utc_now,
)


class AgentService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.polymarket = PolymarketConnector(settings)
        self.external = ExternalFeedConnector()
        self.research = ResearchEngine()
        self.scoring = ScoringEngine(settings)
        self.risk = RiskEngine(settings)
        self.execution = ExecutionEngine(ExecutionMode(settings.trading_mode))
        self.journal = Journal(settings.db_path, settings.events_path)

    def discover_markets(self):
        markets = self.polymarket.discover_markets()
        self.journal.log_event("discover_markets", {"count": len(markets)})
        return markets

    def build_market_snapshot(self, market_id: str) -> MarketSnapshot:
        candidate = self.polymarket.get_market(market_id)
        orderbook = self.polymarket.get_orderbook_snapshot(candidate.yes_token_id)
        seconds_to_expiry = self.polymarket.estimate_seconds_to_expiry(candidate.end_date_iso)
        external_price = self.external.get_btc_price()
        snapshot = MarketSnapshot(
            candidate=candidate,
            orderbook=OrderBookSnapshot(
                bid=orderbook.bid,
                ask=orderbook.ask,
                midpoint=orderbook.midpoint,
                spread=orderbook.spread,
                depth_usd=orderbook.depth_usd,
                last_trade_price=orderbook.last_trade_price,
            ),
            seconds_to_expiry=seconds_to_expiry,
            recent_price_change_bps=(orderbook.midpoint - candidate.implied_probability) * 10_000,
            recent_trade_count=0,
            external_price=external_price,
        )
        self.journal.log_event("market_snapshot", snapshot)
        return snapshot

    def analyze_market(self, market_id: str) -> tuple[MarketSnapshot, MarketAssessment]:
        snapshot = self.build_market_snapshot(market_id)
        packet = self.research.build_evidence_packet(snapshot)
        self.journal.log_event("evidence_packet", packet)
        assessment = self.scoring.score_market(packet)
        self.journal.log_event("market_assessment", assessment)
        return snapshot, assessment

    def paper_trade(self, market_id: str):
        snapshot, assessment = self.analyze_market(market_id)
        account_state = AccountState(
            mode=ExecutionMode.PAPER,
            available_usd=self.settings.max_position_usd * 10,
            open_positions=0,
            daily_realized_pnl=0.0,
        )
        decision = self.risk.decide_trade(snapshot, assessment, account_state)
        self.journal.log_event("trade_decision", decision)
        result = self.execution.execute_trade(decision)
        self.journal.log_event("execution_result", result)
        return snapshot, assessment, decision, result

    def generate_operator_report(self, session_id: str | None = None) -> Report:
        reports = self.journal.read_reports()
        items = [f"{row[2]} | {row[0]} | {row[1]}" for row in reports]
        if not items:
            items = ["No stored reports yet. Run paper or analyze commands first."]
        report = Report(
            session_id=session_id or str(uuid.uuid4()),
            generated_at=utc_now(),
            summary="Recent operator-visible reports and run summaries.",
            items=items,
        )
        self.journal.save_report(report.session_id, report.summary)
        return report

    def status(self) -> dict:
        return {
            "trading_mode": self.settings.trading_mode,
            "market_family": self.settings.market_family,
            "loop_seconds": self.settings.loop_seconds,
            "openrouter_configured": bool(self.settings.openrouter_api_key),
            "db_path": str(self.settings.db_path),
            "events_path": str(self.settings.events_path),
        }
