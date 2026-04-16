from __future__ import annotations

import uuid

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.connectors.external_feeds import ExternalFeedConnector
from polymarket_ai_agent.connectors.polymarket import PolymarketConnector
from polymarket_ai_agent.engine.execution import ExecutionEngine
from polymarket_ai_agent.engine.journal import Journal
from polymarket_ai_agent.engine.portfolio import PortfolioEngine
from polymarket_ai_agent.engine.research import ResearchEngine
from polymarket_ai_agent.engine.risk import RiskEngine
from polymarket_ai_agent.engine.scoring import ScoringEngine
from polymarket_ai_agent.types import (
    AccountState,
    ExecutionMode,
    MarketAssessment,
    MarketSnapshot,
    OrderBookSnapshot,
    PositionAction,
    Report,
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
        self.portfolio = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)

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
        account_state = self.portfolio.get_account_state(ExecutionMode.PAPER)
        decision = self.risk.decide_trade(snapshot, assessment, account_state)
        self.journal.log_event("trade_decision", decision)
        result = self.execution.execute_trade(decision)
        self.portfolio.record_execution(decision, result)
        self.journal.log_event("execution_result", result)
        return snapshot, assessment, decision, result

    def manage_open_positions(self) -> list[PositionAction]:
        actions: list[PositionAction] = []
        due_positions = self.portfolio.positions_due_for_close(self.settings.paper_position_ttl_seconds)
        for position in due_positions:
            snapshot = self.build_market_snapshot(position.market_id)
            action = self.portfolio.close_position(
                position.market_id,
                exit_price=snapshot.orderbook.midpoint,
                reason="ttl_expired",
            )
            self.journal.log_event("position_action", action)
            actions.append(action)
        return actions

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
        auth_status = self.polymarket.get_auth_status()
        return {
            "trading_mode": self.settings.trading_mode,
            "market_family": self.settings.market_family,
            "loop_seconds": self.settings.loop_seconds,
            "openrouter_configured": bool(self.settings.openrouter_api_key),
            "db_path": str(self.settings.db_path),
            "events_path": str(self.settings.events_path),
            "open_positions": len(self.portfolio.list_open_positions()),
            "paper_position_ttl_seconds": self.settings.paper_position_ttl_seconds,
            "auth": {
                "private_key_configured": auth_status.private_key_configured,
                "funder_configured": auth_status.funder_configured,
                "signature_type": auth_status.signature_type,
                "live_client_constructible": auth_status.live_client_constructible,
                "missing": auth_status.missing,
            },
        }
