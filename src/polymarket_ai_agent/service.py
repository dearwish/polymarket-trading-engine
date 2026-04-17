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
            live_trading_enabled=settings.live_trading_enabled,
            live_executor=self.polymarket.execute_live_trade,
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
                two_sided=orderbook.two_sided,
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

    def _prepare_trade(self, market_id: str, mode: ExecutionMode):
        snapshot, assessment = self.analyze_market(market_id)
        account_state = self.portfolio.get_account_state(mode)
        decision = self.risk.decide_trade(snapshot, assessment, account_state)
        return snapshot, assessment, decision, account_state

    def paper_trade(self, market_id: str):
        snapshot, assessment, decision, _account_state = self._prepare_trade(market_id, ExecutionMode.PAPER)
        self.journal.log_event("trade_decision", decision)
        result = self.execution.execute_trade(decision, snapshot.orderbook)
        self.portfolio.record_execution(decision, result)
        self.journal.log_event("execution_result", result)
        return snapshot, assessment, decision, result

    def simulate_market(self, market_id: str):
        snapshot, assessment, decision, _account_state = self._prepare_trade(market_id, ExecutionMode.PAPER)
        self.journal.log_event("simulation_decision", decision)
        return snapshot, assessment, decision

    def live_preflight(self, market_id: str | None = None) -> dict:
        resolved_market_id = market_id or self.get_active_market_id()
        auth = self._auth_status_dict(self.polymarket.probe_live_readiness())
        snapshot, assessment, decision, account_state = self._prepare_trade(resolved_market_id, ExecutionMode.LIVE)
        blockers: list[str] = []
        if self.settings.trading_mode != ExecutionMode.LIVE.value:
            blockers.append("trading_mode_not_live")
        if not self.settings.live_trading_enabled:
            blockers.append("live_trading_disabled")
        if not auth["readonly_ready"]:
            blockers.append("auth_not_ready")
        safety_stop = self.safety_stop_reason(account_state)
        if safety_stop:
            blockers.append(safety_stop)
        if decision.status.value != "APPROVED":
            blockers.extend(decision.rejected_by or ["decision_not_approved"])
        ready = not blockers
        preflight = {
            "readonly": True,
            "market_id": resolved_market_id,
            "ready": ready,
            "blockers": blockers,
            "auth": auth,
            "market": {
                "question": snapshot.candidate.question,
                "slug": snapshot.candidate.slug,
                "implied_probability": snapshot.candidate.implied_probability,
                "liquidity_usd": snapshot.candidate.liquidity_usd,
                "volume_24h_usd": snapshot.candidate.volume_24h_usd,
                "seconds_to_expiry": snapshot.seconds_to_expiry,
            },
            "orderbook": {
                "bid": snapshot.orderbook.bid,
                "ask": snapshot.orderbook.ask,
                "midpoint": snapshot.orderbook.midpoint,
                "spread": snapshot.orderbook.spread,
                "depth_usd": snapshot.orderbook.depth_usd,
                "last_trade_price": snapshot.orderbook.last_trade_price,
                "two_sided": snapshot.orderbook.two_sided,
            },
            "decision": {
                "status": decision.status.value,
                "side": decision.side.value,
                "size_usd": decision.size_usd,
                "limit_price": decision.limit_price,
                "asset_id": decision.asset_id,
                "rejected_by": decision.rejected_by,
            },
            "assessment": {
                "fair_probability": assessment.fair_probability,
                "confidence": assessment.confidence,
                "edge": assessment.edge,
                "suggested_side": assessment.suggested_side.value,
            },
            "account_state": {
                "available_usd": account_state.available_usd,
                "open_positions": account_state.open_positions,
                "daily_realized_pnl": account_state.daily_realized_pnl,
                "rejected_orders": account_state.rejected_orders,
            },
        }
        self.journal.log_event("live_preflight", preflight)
        return preflight

    def live_trade(self, market_id: str):
        preflight = self.live_preflight(market_id)
        if preflight["blockers"]:
            raise RuntimeError(f"Live preflight failed: {', '.join(preflight['blockers'])}")
        snapshot, assessment, decision, _account_state = self._prepare_trade(market_id, ExecutionMode.LIVE)
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

    def run_simulation_cycle(self, market_id: str) -> dict:
        snapshot, assessment, decision = self.simulate_market(market_id)
        cycle = {
            "market_id": snapshot.candidate.market_id,
            "question": snapshot.candidate.question,
            "market_implied_probability": snapshot.candidate.implied_probability,
            "fair_probability": assessment.fair_probability,
            "confidence": assessment.confidence,
            "edge": assessment.edge,
            "suggested_side": assessment.suggested_side.value,
            "decision_status": decision.status.value,
            "decision_side": decision.side.value,
            "limit_price": decision.limit_price,
            "size_usd": decision.size_usd,
            "rejected_by": decision.rejected_by,
            "readonly": True,
        }
        self.journal.log_event("simulation_cycle", cycle)
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
        if "question" in payload and "decision_status" in payload and payload.get("readonly") is True:
            rejected_by = payload.get("rejected_by") or []
            rejected_text = ",".join(rejected_by) if rejected_by else "none"
            return (
                f"{payload['question']} | implied={payload.get('market_implied_probability', 0.0):.4f} "
                f"| fair={payload.get('fair_probability', 0.0):.4f} "
                f"| conf={payload.get('confidence', 0.0):.2f} "
                f"| edge={payload.get('edge', 0.0):.4f} "
                f"| suggested={payload.get('suggested_side', 'n/a')} "
                f"| decision={payload['decision_status']} "
                f"| rejected_by={rejected_text}"
            )
        if "fair_probability" in payload and "confidence" in payload and "edge" in payload:
            return (
                f"market_id={payload.get('market_id', 'n/a')} "
                f"| fair={payload['fair_probability']:.4f} "
                f"| conf={payload['confidence']:.2f} "
                f"| edge={payload['edge']:.4f} "
                f"| suggested={payload.get('suggested_side', 'n/a')}"
            )
        if "status" in payload and "rejected_by" in payload:
            rejected_by = payload.get("rejected_by") or []
            rejected_text = ",".join(rejected_by) if rejected_by else "none"
            return (
                f"market_id={payload.get('market_id', 'n/a')} "
                f"| status={payload['status']} "
                f"| side={payload.get('side', 'n/a')} "
                f"| size={payload.get('size_usd', 0.0):.2f} "
                f"| rejected_by={rejected_text}"
            )
        if "candidate" in payload and "orderbook" in payload:
            candidate = payload["candidate"]
            orderbook = payload["orderbook"]
            return (
                f"{candidate.get('question', 'n/a')} "
                f"| midpoint={orderbook.get('midpoint', 0.0):.4f} "
                f"| spread={orderbook.get('spread', 0.0):.4f} "
                f"| depth={orderbook.get('depth_usd', 0.0):.2f} "
                f"| two_sided={orderbook.get('two_sided', True)} "
                f"| ttl={payload.get('seconds_to_expiry', -1)}s"
            )
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
            "live_trading_enabled": self.settings.live_trading_enabled,
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

    def doctor(self, market_id: str | None = None) -> dict:
        resolved_market_id = market_id or self.get_active_market_id()
        auth = self._auth_status_dict(self.polymarket.probe_live_readiness())
        snapshot, assessment, decision = self.simulate_market(resolved_market_id)
        return {
            "readonly": True,
            "market_id": resolved_market_id,
            "auth": auth,
            "market": {
                "question": snapshot.candidate.question,
                "slug": snapshot.candidate.slug,
                "implied_probability": snapshot.candidate.implied_probability,
                "liquidity_usd": snapshot.candidate.liquidity_usd,
                "volume_24h_usd": snapshot.candidate.volume_24h_usd,
                "seconds_to_expiry": snapshot.seconds_to_expiry,
            },
            "orderbook": {
                "bid": snapshot.orderbook.bid,
                "ask": snapshot.orderbook.ask,
                "midpoint": snapshot.orderbook.midpoint,
                "spread": snapshot.orderbook.spread,
                "depth_usd": snapshot.orderbook.depth_usd,
                "last_trade_price": snapshot.orderbook.last_trade_price,
                "two_sided": snapshot.orderbook.two_sided,
            },
            "simulation": {
                "fair_probability": assessment.fair_probability,
                "confidence": assessment.confidence,
                "edge": assessment.edge,
                "suggested_side": assessment.suggested_side.value,
                "decision_status": decision.status.value,
                "decision_side": decision.side.value,
                "size_usd": decision.size_usd,
                "limit_price": decision.limit_price,
                "rejected_by": decision.rejected_by,
            },
        }

    def live_orders(self) -> dict:
        auth = self._auth_status_dict(self.polymarket.probe_live_readiness())
        if not auth["readonly_ready"]:
            raise RuntimeError("Authenticated live order inspection requires readonly_ready auth.")
        orders = self.polymarket.list_live_orders()
        return {
            "readonly": True,
            "count": len(orders),
            "orders": orders,
        }

    def live_order_status(self, order_id: str) -> dict:
        auth = self._auth_status_dict(self.polymarket.probe_live_readiness())
        if not auth["readonly_ready"]:
            raise RuntimeError("Authenticated live order inspection requires readonly_ready auth.")
        order = self.polymarket.get_live_order(order_id)
        return {
            "readonly": True,
            "order": order,
        }

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
            "collateral_address": auth_status.collateral_address,
            "balance": auth_status.balance,
            "allowance": auth_status.allowance,
            "open_orders_count": auth_status.open_orders_count,
            "open_orders_markets": auth_status.open_orders_markets,
            "diagnostics_collected": auth_status.diagnostics_collected,
            "errors": auth_status.errors,
        }
