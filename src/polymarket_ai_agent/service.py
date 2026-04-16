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
        self.execution = ExecutionEngine(
            ExecutionMode(settings.trading_mode),
            paper_entry_slippage_bps=settings.paper_entry_slippage_bps,
        )
        self.journal = Journal(settings.db_path, settings.events_path)
        self.portfolio = PortfolioEngine(settings.db_path, settings.paper_starting_balance_usd)

    def discover_markets(self):
        markets = self.polymarket.discover_markets()
        self.journal.log_event("discover_markets", {"count": len(markets)})
        return markets

    def get_active_market_id(self) -> str:
        market = self.polymarket.discover_active_market()
        if not market:
            raise RuntimeError("No active market matched the configured market family.")
        self.journal.log_event("active_market", {"market_id": market.market_id})
        return market.market_id

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
        result = self.execution.execute_trade(decision, snapshot.orderbook)
        self.portfolio.record_execution(decision, result)
        self.journal.log_event("execution_result", result)
        return snapshot, assessment, decision, result

    def run_cycle(self, market_id: str) -> dict:
        actions = self.manage_open_positions()
        snapshot, assessment, decision, result = self.paper_trade(market_id)
        cycle = {
            "managed_actions": [
                {"market_id": action.market_id, "action": action.action, "reason": action.reason}
                for action in actions
            ],
            "paper_trade": {
                "market_id": snapshot.candidate.market_id,
                "decision_status": decision.status.value,
                "decision_side": decision.side.value,
                "execution_status": result.status,
                "execution_success": result.success,
                "fill_price": result.fill_price,
            },
        }
        self.journal.log_event("cycle_result", cycle)
        return cycle

    def manage_open_positions(self) -> list[PositionAction]:
        actions: list[PositionAction] = []
        due_positions = self.portfolio.positions_due_for_close(self.settings.paper_position_ttl_seconds)
        for position in due_positions:
            snapshot = self.build_market_snapshot(position.market_id)
            exit_price = self.portfolio.estimate_exit_price(
                position,
                snapshot.orderbook,
                self.settings.paper_exit_slippage_bps,
            )
            action = self.portfolio.close_position(
                position.market_id,
                exit_price=exit_price,
                reason="ttl_expired",
            )
            self.journal.log_event("position_action", action)
            actions.append(action)
        return actions

    def close_position(self, market_id: str, reason: str = "manual_close") -> PositionAction:
        existing = self.portfolio.get_open_position(market_id)
        if not existing:
            action = PositionAction(market_id=market_id, action="NOOP", reason="Position not open.")
            self.journal.log_event("position_action", action)
            return action
        snapshot = self.build_market_snapshot(market_id)
        exit_price = self.portfolio.estimate_exit_price(
            existing,
            snapshot.orderbook,
            self.settings.paper_exit_slippage_bps,
        )
        action = self.portfolio.close_position(
            market_id,
            exit_price=exit_price,
            reason=reason,
        )
        self.journal.log_event("position_action", action)
        return action

    def generate_operator_report(self, session_id: str | None = None) -> Report:
        reports = self.journal.read_reports()
        recent_events = self.journal.read_recent_events(limit=10)
        open_positions = self.portfolio.list_open_positions()
        closed_positions = self.portfolio.list_closed_positions(limit=5)
        items = [f"{row[2]} | {row[0]} | {row[1]}" for row in reports]
        items.extend(
            [
                f"EVENT | {event['logged_at']} | {event['event_type']} | {self._format_event_payload(event['payload'])}"
                for event in recent_events
            ]
        )
        items.extend(
            [
                f"OPEN | {position.market_id} | {position.side.value} | size={position.size_usd:.2f} | entry={position.entry_price:.4f}"
                for position in open_positions
            ]
        )
        items.extend(
            [
                f"CLOSED | {position.market_id} | pnl={position.realized_pnl:.4f} | reason={position.close_reason or 'n/a'}"
                for position in closed_positions
            ]
        )
        if not items:
            items = ["No stored reports yet. Run paper or analyze commands first."]
        report = Report(
            session_id=session_id or str(uuid.uuid4()),
            generated_at=utc_now(),
            summary=f"Open positions: {len(open_positions)} | recently closed: {len(closed_positions)}",
            items=items,
        )
        self.journal.save_report(report.session_id, report.summary)
        return report

    @staticmethod
    def _format_event_payload(payload: dict) -> str:
        if "market_id" in payload:
            return f"market_id={payload['market_id']}"
        if "count" in payload:
            return f"count={payload['count']}"
        if "paper_trade" in payload:
            return f"paper_trade_status={payload['paper_trade'].get('execution_status', 'unknown')}"
        return ",".join(sorted(payload.keys()))[:120]

    def status(self) -> dict:
        auth_status = self.polymarket.probe_live_readiness()
        account_state = self.portfolio.get_account_state(ExecutionMode(self.settings.trading_mode))
        safety_stop_reason = self.safety_stop_reason(account_state)
        return {
            "trading_mode": self.settings.trading_mode,
            "market_family": self.settings.market_family,
            "loop_seconds": self.settings.loop_seconds,
            "openrouter_configured": bool(self.settings.openrouter_api_key),
            "db_path": str(self.settings.db_path),
            "events_path": str(self.settings.events_path),
            "open_positions": account_state.open_positions,
            "available_usd": account_state.available_usd,
            "daily_realized_pnl": account_state.daily_realized_pnl,
            "rejected_orders": account_state.rejected_orders,
            "daily_loss_limit_reached": account_state.daily_realized_pnl <= -self.settings.max_daily_loss_usd,
            "safety_stop_reason": safety_stop_reason,
            "paper_position_ttl_seconds": self.settings.paper_position_ttl_seconds,
            "auth": {
                **self._auth_status_dict(auth_status),
            },
        }

    def auth_status(self) -> dict:
        return self._auth_status_dict(self.polymarket.probe_live_readiness())

    def safety_stop_reason(self, account_state: AccountState | None = None) -> str | None:
        state = account_state or self.portfolio.get_account_state(ExecutionMode(self.settings.trading_mode))
        if state.daily_realized_pnl <= -self.settings.max_daily_loss_usd:
            return "daily_loss_limit"
        if state.rejected_orders >= self.settings.max_rejected_orders:
            return "rejected_order_limit"
        return None

    @staticmethod
    def _auth_status_dict(auth_status) -> dict:
        return {
            "private_key_configured": auth_status.private_key_configured,
            "funder_configured": auth_status.funder_configured,
            "signature_type": auth_status.signature_type,
            "live_client_constructible": auth_status.live_client_constructible,
            "missing": auth_status.missing,
            "wallet_address": auth_status.wallet_address,
            "api_credentials_derived": auth_status.api_credentials_derived,
            "server_ok": auth_status.server_ok,
            "readonly_ready": auth_status.readonly_ready,
            "probe_attempted": auth_status.probe_attempted,
            "errors": auth_status.errors,
        }
