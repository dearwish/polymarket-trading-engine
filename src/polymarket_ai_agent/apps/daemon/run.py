from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from polymarket_ai_agent.apps.daemon.heartbeat import HeartbeatWriter
from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.connectors.binance_ws import BinanceBtcFeed, BtcTick
from polymarket_ai_agent.connectors.polymarket_ws import MarketStreamEvent, PolymarketMarketStream
from polymarket_ai_agent.engine.btc_state import BtcSnapshot, BtcState
from polymarket_ai_agent.engine.market_state import MarketFeatures, MarketState
from polymarket_ai_agent.engine.quant_scoring import QuantScoringEngine
from polymarket_ai_agent.engine.research import ResearchEngine
from polymarket_ai_agent.service import AgentService
from polymarket_ai_agent.types import (
    DecisionStatus,
    EvidencePacket,
    ExecutionMode,
    MarketAssessment,
    MarketCandidate,
    MarketSnapshot,
    OrderBookSnapshot,
    SuggestedSide,
)

logger = logging.getLogger(__name__)

# Window length (in seconds) of each "Up or Down" candle-style family. Used to
# reconstruct the candle-open timestamp from a market's end_date_iso so the
# scorer can compute log(BTC_now / BTC_at_candle_open) — the drift the GBM
# model actually needs for a close > open binary outcome. Threshold markets
# (btc_daily_threshold) use ln(S/K) instead and are not in this map.
_FAMILY_WINDOW_SECONDS: dict[str, int] = {
    "btc_5m": 5 * 60,
    "btc_15m": 15 * 60,
    "btc_1h": 60 * 60,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class DaemonMetrics:
    started_at: datetime = field(default_factory=_utc_now)
    discovery_cycles: int = 0
    discovery_errors: int = 0
    active_market_count: int = 0
    polymarket_events: int = 0
    polymarket_reconnects: int = 0
    btc_ticks: int = 0
    btc_reconnects: int = 0
    decision_ticks: int = 0
    last_polymarket_event_at: datetime | None = None
    last_btc_tick_at: datetime | None = None
    last_decision_at: datetime | None = None
    last_decision_latency_ms: float = 0.0
    maintenance_runs: int = 0
    last_maintenance_at: datetime | None = None
    last_maintenance_summary: dict[str, Any] | None = None
    safety_stop_reason: str | None = None
    safety_stop_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "started_at",
            "last_polymarket_event_at",
            "last_btc_tick_at",
            "last_decision_at",
            "last_maintenance_at",
            "safety_stop_at",
        ):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload


@dataclass(slots=True)
class DaemonConfig:
    market_family: str
    discovery_interval_seconds: float = 60.0
    decision_min_interval_seconds: float = 1.0
    max_active_markets: int = 4
    heartbeat_interval_seconds: float = 5.0
    maintenance_interval_seconds: float = 3600.0
    prune_history_days: int = 14


class MarketStreamFactory(Protocol):
    def __call__(self, url: str) -> PolymarketMarketStream: ...


class BtcFeedFactory(Protocol):
    def __call__(self) -> BinanceBtcFeed: ...


@dataclass(slots=True)
class DecisionContext:
    market_id: str
    candidate: MarketCandidate
    features: MarketFeatures
    btc_snapshot: BtcSnapshot | None
    assessment: MarketAssessment
    metrics: "DaemonMetrics"
    packet: "EvidencePacket | None" = None


DecisionCallback = Callable[[DecisionContext], Awaitable[None]]


class DaemonRunner:
    """Asyncio runner for event-driven Polymarket + BTC market state.

    Phase 1 scope: subscribe to both feeds, keep per-market :class:`MarketState`
    + shared :class:`BtcState` up to date, fire a decision callback at most
    every ``decision_min_interval_seconds``. No orders are placed here — the
    callback (defaults to journaling a `daemon_tick`) is where future phases
    hook in a real strategy.
    """

    def __init__(
        self,
        settings: Settings,
        service: AgentService,
        config: DaemonConfig | None = None,
        market_stream_factory: MarketStreamFactory | None = None,
        btc_feed_factory: BtcFeedFactory | None = None,
        decision_callback: DecisionCallback | None = None,
    ):
        self.settings = settings
        self.service = service
        self.config = config or DaemonConfig(
            market_family=settings.market_family,
            discovery_interval_seconds=float(settings.daemon_discovery_interval_seconds),
            decision_min_interval_seconds=float(settings.daemon_decision_min_interval_seconds),
            heartbeat_interval_seconds=float(settings.daemon_heartbeat_interval_seconds),
            maintenance_interval_seconds=float(settings.daemon_maintenance_interval_seconds),
            prune_history_days=int(settings.daemon_prune_history_days),
        )
        self._market_stream_factory = market_stream_factory or (
            lambda url: PolymarketMarketStream(
                url,
                reconnect_backoff_seconds=settings.ws_reconnect_backoff_seconds,
                reconnect_backoff_max_seconds=settings.ws_reconnect_backoff_max_seconds,
                ssl_verify=settings.ws_ssl_verify,
            )
        )
        self._btc_feed_factory = btc_feed_factory or (
            lambda: BinanceBtcFeed(
                ws_url=settings.btc_ws_url,
                rest_url=settings.btc_rest_fallback_url,
                symbol=settings.btc_symbol,
                reconnect_backoff_seconds=settings.ws_reconnect_backoff_seconds,
                reconnect_backoff_max_seconds=settings.ws_reconnect_backoff_max_seconds,
                ssl_verify=settings.ws_ssl_verify,
            )
        )
        if decision_callback is not None:
            self._decision_callback = decision_callback
        elif settings.daemon_auto_paper_execute:
            self._decision_callback = self._paper_execute_decision_callback
        else:
            self._decision_callback = self._default_decision_callback
        self.metrics = DaemonMetrics()
        self.btc_state = BtcState()
        self.research = ResearchEngine()
        self.quant = QuantScoringEngine(settings)
        self.heartbeat = HeartbeatWriter(settings.heartbeat_path)
        self._market_states: dict[str, MarketState] = {}
        self._candidates: dict[str, MarketCandidate] = {}
        self._asset_to_market: dict[str, str] = {}
        self._active_asset_ids: set[str] = set()
        self._market_subscriber_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._last_decision_at: datetime | None = None
        # Per-open-position state used by trailing stop / tranche ladder logic.
        # Paper-mode only: lives in memory, reset if the daemon restarts.
        self._position_extras: dict[str, dict[str, float]] = {}
        self._tp_ladder: list[tuple[float, float]] = self._parse_tp_ladder(settings.paper_tp_ladder)
        # Last close timestamp per market, used to enforce an entry cooldown
        # that blocks whipsaw re-entries on the same market. In-memory only.
        self._last_close_at: dict[str, datetime] = {}

    @property
    def active_asset_ids(self) -> list[str]:
        return sorted(self._active_asset_ids)

    @property
    def active_market_ids(self) -> list[str]:
        return sorted(self._market_states.keys())

    def features_snapshot(self) -> dict[str, MarketFeatures]:
        return {market_id: state.features() for market_id, state in self._market_states.items()}

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        self._stop_event = stop_event or asyncio.Event()
        discovery_task = asyncio.create_task(self._discovery_loop(self._stop_event))
        btc_task = asyncio.create_task(self._btc_loop(self._stop_event))
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(self._stop_event))
        maintenance_task = asyncio.create_task(self._maintenance_loop(self._stop_event))
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_tasks(
                [
                    discovery_task,
                    btc_task,
                    heartbeat_task,
                    maintenance_task,
                    self._market_subscriber_task,
                ]
            )

    async def run_for(self, duration_seconds: float) -> None:
        stop_event = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(duration_seconds)
            stop_event.set()

        stopper_task = asyncio.create_task(stopper())
        try:
            await self.run(stop_event)
        finally:
            stopper_task.cancel()
            try:
                await stopper_task
            except (asyncio.CancelledError, Exception):
                pass

    # --- Discovery -----------------------------------------------------

    async def _discovery_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                candidates = await asyncio.to_thread(self._discover_candidates)
                await self._apply_candidates(candidates)
                self.metrics.discovery_cycles += 1
            except Exception as exc:
                self.metrics.discovery_errors += 1
                logger.warning("daemon discovery failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.discovery_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _discover_candidates(self) -> list[MarketCandidate]:
        markets = self.service.discover_markets()
        return markets[: self.config.max_active_markets]

    async def _apply_candidates(self, candidates: Iterable[MarketCandidate]) -> None:
        new_states: dict[str, MarketState] = {}
        new_candidates: dict[str, MarketCandidate] = {}
        asset_to_market: dict[str, str] = {}
        asset_ids: set[str] = set()
        for candidate in candidates:
            state = self._market_states.get(candidate.market_id) or MarketState(
                market_id=candidate.market_id,
                yes_token_id=candidate.yes_token_id,
                no_token_id=candidate.no_token_id,
            )
            new_states[candidate.market_id] = state
            new_candidates[candidate.market_id] = candidate
            if candidate.yes_token_id:
                asset_to_market[candidate.yes_token_id] = candidate.market_id
                asset_ids.add(candidate.yes_token_id)
            if candidate.no_token_id:
                asset_to_market[candidate.no_token_id] = candidate.market_id
                asset_ids.add(candidate.no_token_id)
        self._market_states = new_states
        self._candidates = new_candidates
        self._asset_to_market = asset_to_market
        self.metrics.active_market_count = len(new_states)
        if asset_ids == self._active_asset_ids:
            return
        self._active_asset_ids = asset_ids
        await self._restart_market_subscriber()

    async def _restart_market_subscriber(self) -> None:
        assert self._stop_event is not None
        if self._market_subscriber_task is not None:
            self._market_subscriber_task.cancel()
            try:
                await self._market_subscriber_task
            except (asyncio.CancelledError, Exception):
                pass
            self._market_subscriber_task = None
        if not self._active_asset_ids:
            return
        self._market_subscriber_task = asyncio.create_task(
            self._polymarket_loop(self._stop_event, list(self._active_asset_ids))
        )

    # --- Polymarket WS -------------------------------------------------

    async def _polymarket_loop(self, stop_event: asyncio.Event, asset_ids: list[str]) -> None:
        stream = self._market_stream_factory(self.settings.polymarket_ws_market_url)
        iterator = stream.run(asset_ids, stop_event=stop_event)
        async for event in iterator:
            if stop_event.is_set():
                break
            await self._on_polymarket_event(event)

    async def _on_polymarket_event(self, event: MarketStreamEvent) -> None:
        self.metrics.polymarket_events += 1
        self.metrics.last_polymarket_event_at = _utc_now()
        payload = event.payload
        asset_id = str(payload.get("asset_id") or "")
        market_id = self._asset_to_market.get(asset_id)
        if market_id is None:
            return
        state = self._market_states.get(market_id)
        if state is None:
            return
        if event.event_type == "book":
            state.apply_book_snapshot(payload)
        elif event.event_type == "price_change":
            state.apply_price_change(payload)
        elif event.event_type in {"last_trade_price", "trade"}:
            state.apply_last_trade(payload)
        await self._maybe_fire_decision(state)

    # --- BTC WS --------------------------------------------------------

    async def _btc_loop(self, stop_event: asyncio.Event) -> None:
        feed = self._btc_feed_factory()
        # Seed from REST so features are usable before the first tick arrives.
        tick = await asyncio.to_thread(feed.rest_price)
        if tick is not None:
            self.btc_state.record(tick.price, tick.observed_at)
        async for tick in self._iter_btc(feed, stop_event):
            if stop_event.is_set():
                break
            self.metrics.btc_ticks += 1
            self.metrics.last_btc_tick_at = tick.observed_at
            self.btc_state.record(tick.price, tick.observed_at)

    async def _iter_btc(self, feed: BinanceBtcFeed, stop_event: asyncio.Event) -> AsyncIterator[BtcTick]:
        async for tick in feed.run(stop_event=stop_event):
            yield tick

    # --- Decision gating ----------------------------------------------

    async def _maybe_fire_decision(self, state: MarketState) -> None:
        now = _utc_now()
        if self._last_decision_at is not None:
            elapsed = (now - self._last_decision_at).total_seconds()
            if elapsed < self.config.decision_min_interval_seconds:
                return
        if self.metrics.safety_stop_reason is not None:
            # Kill-switch is active — skip the callback so no new trade
            # decisions are published. We still update the timer so we only
            # log once per interval when the switch is hot.
            self._last_decision_at = now
            return
        candidate = self._candidates.get(state.market_id)
        if candidate is None:
            return
        self._last_decision_at = now
        self.metrics.decision_ticks += 1
        self.metrics.last_decision_at = now
        started = _utc_now()
        features = state.features(now=now)
        btc_snapshot = self.btc_state.snapshot(now=now)
        tte_seconds = self._seconds_to_expiry(candidate.end_date_iso, now=now)
        # Compute BTC's log-return since THIS market's candle opened so the
        # scorer's GBM uses Δ_observed (correct) instead of a rolling window.
        window_len = _FAMILY_WINDOW_SECONDS.get(self.settings.market_family, 0)
        time_elapsed = max(0, window_len - tte_seconds) if window_len > 0 else 0
        candle_open_log_return = 0.0
        if time_elapsed > 0 and self.btc_state.sample_count > 1:
            candle_open_log_return = self.btc_state.log_return_over(time_elapsed, now=now)
        packet = self.research.build_from_features(
            candidate=candidate,
            features=features,
            btc_snapshot=btc_snapshot,
            seconds_to_expiry=tte_seconds,
            time_elapsed_in_candle_s=int(time_elapsed),
            btc_log_return_since_candle_open=candle_open_log_return,
        )
        assessment = self.quant.score_market(packet)
        context = DecisionContext(
            market_id=state.market_id,
            candidate=candidate,
            features=features,
            btc_snapshot=btc_snapshot,
            assessment=assessment,
            metrics=self.metrics,
            packet=packet,
        )
        try:
            await self._decision_callback(context)
        except Exception as exc:
            logger.warning("daemon decision callback failed: %s", exc)
        elapsed = (_utc_now() - started).total_seconds() * 1000.0
        self.metrics.last_decision_latency_ms = round(elapsed, 3)

    @staticmethod
    def _parse_tp_ladder(raw: str) -> list[tuple[float, float]]:
        """Parse "0.15:0.5,0.30:0.25" into [(0.15, 0.5), (0.30, 0.25)].

        Invalid pairs are silently skipped. Ladder is sorted ascending by
        PnL-pct so the daemon can walk it left-to-right.
        """
        if not raw:
            return []
        pairs: list[tuple[float, float]] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if ":" not in chunk:
                continue
            left, right = chunk.split(":", 1)
            try:
                pct = float(left)
                frac = float(right)
            except ValueError:
                continue
            if pct <= 0 or not (0.0 < frac <= 1.0):
                continue
            pairs.append((pct, frac))
        pairs.sort(key=lambda item: item[0])
        return pairs

    @staticmethod
    def _seconds_to_expiry(end_date_iso: str, now: datetime | None = None) -> int:
        if not end_date_iso:
            return 0
        try:
            expiry = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return 0
        reference = now or _utc_now()
        return max(0, int((expiry - reference).total_seconds()))

    async def _default_decision_callback(self, context: DecisionContext) -> None:
        features = context.features
        btc = context.btc_snapshot
        assessment = context.assessment
        payload: dict[str, Any] = {
            "market_id": features.market_id,
            "question": context.candidate.question,
            "slug": context.candidate.slug,
            "end_date_iso": context.candidate.end_date_iso,
            "seconds_to_expiry": context.assessment and self._seconds_to_expiry(context.candidate.end_date_iso),
            "bid_yes": features.bid_yes,
            "ask_yes": features.ask_yes,
            "mid_yes": features.mid_yes,
            "microprice_yes": features.microprice_yes,
            "imbalance_top5_yes": features.imbalance_top5_yes,
            "depth_usd_yes": features.depth_usd_yes,
            "spread_yes": features.spread_yes,
            "signed_flow_5s": features.signed_flow_5s,
            "trade_count_5s": features.trade_count_5s,
            "last_update_age_seconds": features.last_update_age_seconds,
            "btc_price": btc.price if btc else None,
            "btc_realized_vol_30m": btc.realized_vol_30m if btc else None,
            "btc_log_return_5m": btc.log_return_5m if btc else None,
            "btc_log_return_since_candle_open": context.packet.btc_log_return_since_candle_open if context.packet else None,
            "time_elapsed_in_candle_s": context.packet.time_elapsed_in_candle_s if context.packet else None,
            "polymarket_events": context.metrics.polymarket_events,
            "btc_ticks": context.metrics.btc_ticks,
            "fair_probability": assessment.fair_probability,
            "fair_probability_no": assessment.fair_probability_no,
            "edge_yes": assessment.edge_yes,
            "edge_no": assessment.edge_no,
            "suggested_side": assessment.suggested_side.value,
            "confidence": assessment.confidence,
            "slippage_bps": assessment.slippage_bps,
            "expiry_risk": assessment.expiry_risk,
        }
        await asyncio.to_thread(self.service.journal.log_event, "daemon_tick", payload)

    async def _paper_execute_decision_callback(self, context: DecisionContext) -> None:
        """Log the tick AND run the full paper pipeline: risk → execute → position.

        Skips entry if the market already has an open paper position. Closes an
        existing position when TTE drops inside the per-family exit buffer, so
        the portfolio realises PnL rather than leaving stale open positions.
        """
        await self._default_decision_callback(context)

        market_id = context.market_id
        candidate = context.candidate
        features = context.features
        assessment = context.assessment
        tte_seconds = self._seconds_to_expiry(candidate.end_date_iso)

        orderbook = self._build_orderbook_from_state(market_id, features)
        if orderbook is None:
            return  # No usable book yet — skip this tick.

        # Manage an already-open position.
        # Exit priority (first match wins, checked every tick):
        #   1. TP ladder (partial closes at +X% PnL)
        #   2. Trailing stop (full close if current drops `trail_pct` below peak)
        #   3. Fixed take-profit (full close at +X% PnL)
        #   4. Fixed stop-loss (full close at -Y% PnL)
        #   5. TTE exit buffer (full close near expiry at current mid)
        open_pos = await asyncio.to_thread(self.service.portfolio.get_open_position, market_id)
        if open_pos is not None:
            current_price = features.mid_yes if open_pos.side == SuggestedSide.YES else features.mid_no
            entry_price = float(open_pos.entry_price)
            if current_price > 0.0 and entry_price > 0.0:
                pnl_pct = (current_price - entry_price) / entry_price
                extras = self._position_extras.setdefault(market_id, {"peak_price": 0.0, "tranches_closed": 0.0})
                if current_price > extras["peak_price"]:
                    extras["peak_price"] = current_price
                # --- 1. TP ladder (partial close) -------------------------
                tranches_closed = int(extras["tranches_closed"])
                if tranches_closed < len(self._tp_ladder):
                    next_pct, next_frac = self._tp_ladder[tranches_closed]
                    if pnl_pct >= next_pct:
                        await asyncio.to_thread(
                            self.service.portfolio.partial_close_position,
                            market_id,
                            float(next_frac),
                            float(current_price),
                            f"paper_tp_ladder_{tranches_closed + 1}",
                        )
                        extras["tranches_closed"] = tranches_closed + 1
                        # Partial closes don't start a cooldown — position is
                        # still live for the remainder; cooldown only applies
                        # after a FULL close.
                        return
                # --- 2. Trailing stop (full close) ------------------------
                # Arms only once peak clears entry × (1 + arm_pct); prevents
                # the trail from locking in a small loss when the peak barely
                # moved above entry.
                trail_pct = float(self.settings.paper_trailing_stop_pct)
                arm_pct = float(self.settings.paper_trail_arm_pct)
                peak = extras["peak_price"]
                arm_threshold = entry_price * (1.0 + arm_pct)
                trail_armed = peak >= arm_threshold
                if trail_pct > 0.0 and trail_armed and current_price <= peak * (1.0 - trail_pct):
                    await asyncio.to_thread(
                        self.service.portfolio.close_position,
                        market_id,
                        float(current_price),
                        "paper_trailing_stop",
                    )
                    self._position_extras.pop(market_id, None)
                    self._last_close_at[market_id] = _utc_now()
                    return
                # --- 3 + 4. Fixed TP / SL ---------------------------------
                tp_pct = float(self.settings.paper_take_profit_pct)
                sl_pct = float(self.settings.paper_stop_loss_pct)
                close_reason: str | None = None
                if tp_pct > 0.0 and pnl_pct >= tp_pct:
                    close_reason = "paper_take_profit"
                elif sl_pct > 0.0 and pnl_pct <= -sl_pct:
                    close_reason = "paper_stop_loss"
                if close_reason is not None:
                    await asyncio.to_thread(
                        self.service.portfolio.close_position,
                        market_id,
                        float(current_price),
                        close_reason,
                    )
                    self._position_extras.pop(market_id, None)
                    self._last_close_at[market_id] = _utc_now()
                    return
            # --- 5. TTE exit buffer ---------------------------------------
            exit_buffer = self.service.risk.exit_buffer_seconds_for_tte(tte_seconds)
            if tte_seconds <= exit_buffer and current_price > 0.0:
                await asyncio.to_thread(
                    self.service.portfolio.close_position,
                    market_id,
                    float(current_price),
                    "paper_tte_exit",
                )
                self._position_extras.pop(market_id, None)
                self._last_close_at[market_id] = _utc_now()
            return  # Do not open a duplicate while a position is live.
        # Clean up extras if no open position exists (e.g., previous TTE close).
        self._position_extras.pop(market_id, None)

        # Enforce entry cooldown after a recent close on this market.
        cooldown_seconds = int(self.settings.paper_entry_cooldown_seconds)
        if cooldown_seconds > 0:
            last_close = self._last_close_at.get(market_id)
            if last_close is not None:
                elapsed = (_utc_now() - last_close).total_seconds()
                if elapsed < cooldown_seconds:
                    return

        snapshot = MarketSnapshot(
            candidate=candidate,
            orderbook=orderbook,
            seconds_to_expiry=tte_seconds,
            recent_price_change_bps=0.0,
            recent_trade_count=features.trade_count_5s,
            external_price=context.btc_snapshot.price if context.btc_snapshot else 0.0,
        )
        account_state = await asyncio.to_thread(
            self.service.portfolio.get_account_state, ExecutionMode.PAPER
        )
        decision = self.service.risk.decide_trade(snapshot, assessment, account_state)
        await asyncio.to_thread(self.service.journal.log_event, "trade_decision", decision)
        if decision.status != DecisionStatus.APPROVED:
            return
        result = self.service.execution.execute_trade(
            decision,
            orderbook,
            seconds_to_expiry=tte_seconds,
            edge=assessment.edge,
        )
        await asyncio.to_thread(self.service.portfolio.record_execution, decision, result)
        await asyncio.to_thread(self.service.journal.log_event, "execution_result", result)

    def _build_orderbook_from_state(
        self, market_id: str, features: MarketFeatures
    ) -> OrderBookSnapshot | None:
        """Snapshot the live per-market order book from WS state (no REST call)."""
        state = self._market_states.get(market_id)
        if state is None:
            return None
        yes_book = state.yes_book
        bid_levels = list(yes_book.bids.sorted_levels()[:10])
        ask_levels = list(yes_book.asks.sorted_levels()[:10])
        best_bid = features.bid_yes
        best_ask = features.ask_yes
        if not best_bid and not best_ask:
            return None
        midpoint = features.mid_yes or best_bid or best_ask
        return OrderBookSnapshot(
            bid=best_bid,
            ask=best_ask,
            midpoint=midpoint,
            spread=features.spread_yes,
            depth_usd=features.depth_usd_yes,
            last_trade_price=yes_book.last_trade_price or midpoint,
            two_sided=features.two_sided,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
        )

    # --- Heartbeat + maintenance --------------------------------------

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        """Persist the daemon's metrics at a steady cadence.

        The API process has no in-memory view of the daemon so it reads this
        file to compute heartbeat-age, kill-switch state, and the per-daemon
        metrics exposed on ``/api/metrics``.
        """
        interval = max(0.1, float(self.config.heartbeat_interval_seconds))
        while not stop_event.is_set():
            try:
                auth = self._auth_readonly_ready()
                self._apply_safety_stop(auth_readonly_ready=auth)
                extra = {
                    "active_market_ids": self.active_market_ids,
                    "active_asset_ids": self.active_asset_ids,
                    "btc_last_price": self.btc_state.last_price,
                    "btc_seconds_since_last_update": self.btc_state.seconds_since_last_update(),
                    "auth_readonly_ready": auth,
                    "safety_stop_reason": self.metrics.safety_stop_reason,
                    "market_family": self.settings.market_family,
                }
                await asyncio.to_thread(self.heartbeat.write, self.metrics, extra)
            except Exception as exc:
                logger.warning("daemon heartbeat write failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _maintenance_loop(self, stop_event: asyncio.Event) -> None:
        """Periodic retention + WAL checkpoint + VACUUM-lite upkeep.

        Runs separately from the decision loop so SQLite's exclusive lock for
        VACUUM never blocks a tick. First iteration waits the full interval so
        a freshly started daemon doesn't immediately churn the DB.
        """
        interval = max(60.0, float(self.config.maintenance_interval_seconds))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        while not stop_event.is_set():
            try:
                summary = await asyncio.to_thread(self._run_maintenance)
                self.metrics.maintenance_runs += 1
                self.metrics.last_maintenance_at = _utc_now()
                self.metrics.last_maintenance_summary = summary
            except Exception as exc:
                logger.warning("daemon maintenance run failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def _run_maintenance(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        days = int(self.config.prune_history_days)
        if days > 0:
            summary["history_pruned"] = self.service.portfolio.prune_history(days)
        events_pruned = self.service.journal.prune_events_jsonl(
            self.settings.events_jsonl_max_bytes,
            keep_tail_bytes=self.settings.events_jsonl_keep_tail_bytes,
        )
        summary["events_jsonl_pruned"] = bool(events_pruned)
        try:
            wal = self.service.portfolio.wal_checkpoint()
            summary["wal_checkpoint"] = {
                "busy": wal[0],
                "log_pages": wal[1],
                "checkpointed_pages": wal[2],
            }
        except Exception as exc:
            summary["wal_checkpoint_error"] = str(exc)
        summary["db_size_bytes"] = self.service.journal.db_size_bytes()
        summary["events_jsonl_size_bytes"] = self.service.journal.events_jsonl_size_bytes()
        return summary

    def _auth_readonly_ready(self) -> bool:
        try:
            status = self.service.polymarket.get_auth_status()
        except Exception:
            return False
        return bool(status.live_client_constructible)

    def _apply_safety_stop(self, auth_readonly_ready: bool | None) -> None:
        reason = self.service.safety_stop_reason(
            auth_readonly_ready=auth_readonly_ready,
        )
        if reason is None:
            if self.metrics.safety_stop_reason is not None:
                logger.info("daemon kill-switch cleared (was %s)", self.metrics.safety_stop_reason)
            self.metrics.safety_stop_reason = None
            self.metrics.safety_stop_at = None
            return
        if self.metrics.safety_stop_reason == reason:
            return
        self.metrics.safety_stop_reason = reason
        self.metrics.safety_stop_at = _utc_now()
        logger.warning("daemon kill-switch fired: %s", reason)
        try:
            self.service.journal.log_event("safety_stop", {"reason": reason})
        except Exception as exc:
            logger.warning("failed to journal safety_stop: %s", exc)

    # --- Shutdown ------------------------------------------------------

    async def _shutdown_tasks(self, tasks: Iterable[asyncio.Task[None] | None]) -> None:
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def run_daemon(settings: Settings, service: AgentService, duration_seconds: float | None = None) -> None:
    """Synchronous entry point used by the CLI.

    If ``duration_seconds`` is provided the runner stops after that many
    seconds (useful for smoke tests); otherwise it runs until the process is
    interrupted.
    """
    runner = DaemonRunner(settings=settings, service=service)

    async def _main() -> None:
        if duration_seconds is not None:
            await runner.run_for(duration_seconds)
            return
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        def _request_stop() -> None:
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                # Windows / non-mainloop environments.
                pass
        await runner.run(stop_event)

    asyncio.run(_main())
