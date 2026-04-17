from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.connectors.binance_ws import BinanceBtcFeed, BtcTick
from polymarket_ai_agent.connectors.polymarket_ws import MarketStreamEvent, PolymarketMarketStream
from polymarket_ai_agent.engine.btc_state import BtcSnapshot, BtcState
from polymarket_ai_agent.engine.market_state import MarketFeatures, MarketState
from polymarket_ai_agent.service import AgentService
from polymarket_ai_agent.types import MarketCandidate

logger = logging.getLogger(__name__)


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

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("started_at", "last_polymarket_event_at", "last_btc_tick_at", "last_decision_at"):
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


class MarketStreamFactory(Protocol):
    def __call__(self, url: str) -> PolymarketMarketStream: ...


class BtcFeedFactory(Protocol):
    def __call__(self) -> BinanceBtcFeed: ...


DecisionCallback = Callable[[MarketFeatures, BtcSnapshot | None, DaemonMetrics], Awaitable[None]]


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
        )
        self._market_stream_factory = market_stream_factory or (
            lambda url: PolymarketMarketStream(
                url,
                reconnect_backoff_seconds=settings.ws_reconnect_backoff_seconds,
                reconnect_backoff_max_seconds=settings.ws_reconnect_backoff_max_seconds,
            )
        )
        self._btc_feed_factory = btc_feed_factory or (
            lambda: BinanceBtcFeed(
                ws_url=settings.btc_ws_url,
                rest_url=settings.btc_rest_fallback_url,
                symbol=settings.btc_symbol,
                reconnect_backoff_seconds=settings.ws_reconnect_backoff_seconds,
                reconnect_backoff_max_seconds=settings.ws_reconnect_backoff_max_seconds,
            )
        )
        self._decision_callback = decision_callback or self._default_decision_callback
        self.metrics = DaemonMetrics()
        self.btc_state = BtcState()
        self._market_states: dict[str, MarketState] = {}
        self._asset_to_market: dict[str, str] = {}
        self._active_asset_ids: set[str] = set()
        self._market_subscriber_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._last_decision_at: datetime | None = None

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
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_tasks([discovery_task, btc_task, self._market_subscriber_task])

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
        asset_to_market: dict[str, str] = {}
        asset_ids: set[str] = set()
        for candidate in candidates:
            state = self._market_states.get(candidate.market_id) or MarketState(
                market_id=candidate.market_id,
                yes_token_id=candidate.yes_token_id,
                no_token_id=candidate.no_token_id,
            )
            new_states[candidate.market_id] = state
            if candidate.yes_token_id:
                asset_to_market[candidate.yes_token_id] = candidate.market_id
                asset_ids.add(candidate.yes_token_id)
            if candidate.no_token_id:
                asset_to_market[candidate.no_token_id] = candidate.market_id
                asset_ids.add(candidate.no_token_id)
        self._market_states = new_states
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
        self._last_decision_at = now
        self.metrics.decision_ticks += 1
        self.metrics.last_decision_at = now
        started = _utc_now()
        features = state.features(now=now)
        btc_snapshot = self.btc_state.snapshot(now=now)
        try:
            await self._decision_callback(features, btc_snapshot, self.metrics)
        except Exception as exc:
            logger.warning("daemon decision callback failed: %s", exc)
        elapsed = (_utc_now() - started).total_seconds() * 1000.0
        self.metrics.last_decision_latency_ms = round(elapsed, 3)

    async def _default_decision_callback(
        self,
        features: MarketFeatures,
        btc_snapshot: BtcSnapshot | None,
        metrics: DaemonMetrics,
    ) -> None:
        payload: dict[str, Any] = {
            "market_id": features.market_id,
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
            "btc_price": btc_snapshot.price if btc_snapshot else None,
            "btc_realized_vol_30m": btc_snapshot.realized_vol_30m if btc_snapshot else None,
            "btc_log_return_5m": btc_snapshot.log_return_5m if btc_snapshot else None,
            "polymarket_events": metrics.polymarket_events,
            "btc_ticks": metrics.btc_ticks,
        }
        await asyncio.to_thread(self.service.journal.log_event, "daemon_tick", payload)

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
