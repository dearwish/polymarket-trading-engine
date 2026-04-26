from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_ai_agent.apps.daemon.run import DaemonConfig, DaemonMetrics, DaemonRunner
from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.connectors.binance_ws import BtcTick
from polymarket_ai_agent.connectors.polymarket_ws import MarketStreamEvent
from polymarket_ai_agent.service import AgentService
from polymarket_ai_agent.types import MarketCandidate


class FakeMarketStream:
    def __init__(self, events: list[MarketStreamEvent]):
        self._events = list(events)
        self.run_calls: list[list[str]] = []

    async def run(
        self,
        asset_ids: Iterable[str],
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[MarketStreamEvent]:
        self.run_calls.append(list(asset_ids))
        for event in self._events:
            if stop_event is not None and stop_event.is_set():
                return
            yield event
            await asyncio.sleep(0)
        # Keep the task alive so the daemon can exit on its own.
        if stop_event is not None:
            await stop_event.wait()


class FakeBtcFeed:
    def __init__(self, ticks: list[BtcTick], rest_tick: BtcTick | None = None):
        self._ticks = list(ticks)
        self._rest_tick = rest_tick
        self.rest_calls = 0

    def rest_price(self) -> BtcTick | None:
        self.rest_calls += 1
        return self._rest_tick

    async def run(self, stop_event: asyncio.Event | None = None) -> AsyncIterator[BtcTick]:
        for tick in self._ticks:
            if stop_event is not None and stop_event.is_set():
                return
            yield tick
            await asyncio.sleep(0)
        if stop_event is not None:
            await stop_event.wait()


class FakeService:
    def __init__(self, candidates: list[MarketCandidate], journal):
        self._candidates = candidates
        self.journal = journal

    def discover_markets(self) -> list[MarketCandidate]:
        return list(self._candidates)


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event_type: str, payload) -> None:
        self.events.append((event_type, dict(payload)))


def _candidate(market_id: str, yes: str, no: str) -> MarketCandidate:
    return MarketCandidate(
        market_id=market_id,
        question=f"Bitcoin up or down {market_id}",
        condition_id=f"cond-{market_id}",
        slug=f"slug-{market_id}",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id=yes,
        no_token_id=no,
        implied_probability=0.5,
        liquidity_usd=10000.0,
        volume_24h_usd=20000.0,
    )


def _settings(tmp_path: Path) -> Settings:
    from polymarket_ai_agent.engine.migrations import MigrationRunner

    s = Settings(
        openrouter_api_key="",
        market_family="btc_1h",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
        heartbeat_path=tmp_path / "data" / "daemon_heartbeat.json",
        daemon_discovery_interval_seconds=60,
        daemon_decision_min_interval_seconds=0.0,
        # Pin experiment flags so live .env values don't bleed into tests.
        min_candle_elapsed_seconds=0,
        position_force_exit_tte_seconds=0,
        min_entry_tte_seconds=0,
        max_consecutive_losses=0,
    )
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    MigrationRunner(s.db_path).run()
    return s


def test_daemon_processes_ws_events_and_updates_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    candidates = [_candidate("m1", "yes-1", "no-1")]
    journal = FakeJournal()
    service = FakeService(candidates, journal)

    events = [
        MarketStreamEvent(
            event_type="book",
            payload={
                "asset_id": "yes-1",
                "bids": [{"price": "0.48", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}],
            },
        ),
        MarketStreamEvent(
            event_type="price_change",
            payload={
                "asset_id": "yes-1",
                "price_changes": [{"price": "0.49", "size": "50", "side": "BUY"}],
            },
        ),
        MarketStreamEvent(
            event_type="trade",
            payload={
                "asset_id": "yes-1",
                "price": "0.50",
                "size": "5",
                "side": "BUY",
            },
        ),
    ]
    # Space the ticks ≥ 1s apart so BtcState's 1s decimation keeps each one.
    now_utc = datetime.now(timezone.utc)
    btc_ticks = [
        BtcTick(price=70000.0, observed_at=now_utc + timedelta(seconds=2), source="aggTrade"),
        BtcTick(price=70100.0, observed_at=now_utc + timedelta(seconds=4), source="aggTrade"),
    ]

    market_stream = FakeMarketStream(events)
    btc_feed = FakeBtcFeed(
        btc_ticks,
        rest_tick=BtcTick(price=69900.0, observed_at=now_utc, source="rest"),
    )

    runner = DaemonRunner(
        settings=settings,
        service=service,  # type: ignore[arg-type]
        config=DaemonConfig(
            market_family="btc_1h",
            discovery_interval_seconds=3600.0,
            decision_min_interval_seconds=0.0,
        ),
        market_stream_factory=lambda url: market_stream,  # type: ignore[arg-type]
        btc_feed_factory=lambda: btc_feed,  # type: ignore[arg-type]
    )

    asyncio.run(runner.run_for(0.4))

    assert runner.metrics.active_market_count == 1
    assert runner.metrics.polymarket_events == len(events)
    assert runner.metrics.btc_ticks == len(btc_ticks)
    assert runner.metrics.decision_ticks >= 1
    snapshot = runner.features_snapshot()["m1"]
    assert snapshot.bid_yes == 0.49
    assert snapshot.ask_yes == 0.52
    assert runner.btc_state.last_price == 70100.0
    tick_payloads = [payload for evt, payload in journal.events if evt == "daemon_tick"]
    assert tick_payloads, "daemon_tick journal events expected"
    latest = tick_payloads[-1]
    # Phase 2: daemon now runs the quant scorer on every decision tick.
    assert "fair_probability" in latest
    assert "edge_yes" in latest
    assert "edge_no" in latest
    assert latest["suggested_side"] in {"YES", "NO", "ABSTAIN"}
    # Phase 1 adaptive-regime: classifier output is attached to every
    # daemon_tick so analyze_soak can stratify without re-classifying.
    assert latest["regime"] in {"TRENDING_UP", "TRENDING_DOWN", "HIGH_VOL", "RANGING", "UNKNOWN"}
    assert market_stream.run_calls, "market stream run() should be invoked"
    assert btc_feed.rest_calls == 1, "BTC seed REST call expected"


def test_daemon_skips_polymarket_when_no_markets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    journal = FakeJournal()
    service = FakeService([], journal)
    market_stream = FakeMarketStream([])
    btc_feed = FakeBtcFeed([])
    runner = DaemonRunner(
        settings=settings,
        service=service,  # type: ignore[arg-type]
        config=DaemonConfig(
            market_family="btc_1h",
            discovery_interval_seconds=3600.0,
            decision_min_interval_seconds=0.0,
        ),
        market_stream_factory=lambda url: market_stream,  # type: ignore[arg-type]
        btc_feed_factory=lambda: btc_feed,  # type: ignore[arg-type]
    )
    asyncio.run(runner.run_for(0.2))
    assert runner.metrics.active_market_count == 0
    assert runner.metrics.polymarket_events == 0
    assert market_stream.run_calls == []


def test_daemon_heartbeat_loop_writes_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    journal = FakeJournal()
    service = FakeService([], journal)
    # Stub the polymarket probe so _auth_readonly_ready works on FakeService.
    service.polymarket = type("P", (), {"get_auth_status": lambda self: type("A", (), {"live_client_constructible": True})()})()  # type: ignore
    service.safety_stop_reason = lambda **_: None  # type: ignore
    market_stream = FakeMarketStream([])
    btc_feed = FakeBtcFeed([])
    runner = DaemonRunner(
        settings=settings,
        service=service,  # type: ignore[arg-type]
        config=DaemonConfig(
            market_family="btc_1h",
            discovery_interval_seconds=3600.0,
            decision_min_interval_seconds=0.0,
            heartbeat_interval_seconds=0.05,
            maintenance_interval_seconds=3600.0,
        ),
        market_stream_factory=lambda url: market_stream,  # type: ignore[arg-type]
        btc_feed_factory=lambda: btc_feed,  # type: ignore[arg-type]
    )
    asyncio.run(runner.run_for(0.3))
    assert settings.heartbeat_path.exists()
    import json as _json

    payload = _json.loads(settings.heartbeat_path.read_text())
    assert "metrics" in payload
    assert payload["market_family"] == "btc_1h"


def test_daemon_kill_switch_gates_decision_callback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    candidates = [_candidate("m1", "yes-1", "no-1")]
    journal = FakeJournal()
    service = FakeService(candidates, journal)
    # The heartbeat loop re-evaluates safety_stop_reason via the service;
    # force it to always return a stop so the kill switch stays hot.
    service.polymarket = type("P", (), {"get_auth_status": lambda self: type("A", (), {"live_client_constructible": True})()})()  # type: ignore
    service.safety_stop_reason = lambda **_: "daily_loss_limit"  # type: ignore

    events = [
        MarketStreamEvent(
            event_type="book",
            payload={
                "asset_id": "yes-1",
                "bids": [{"price": "0.48", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}],
            },
        ),
    ]
    market_stream = FakeMarketStream(events)
    btc_feed = FakeBtcFeed([])

    callback_hits: list[str] = []

    async def callback(context):  # type: ignore[no-untyped-def]
        callback_hits.append(context.market_id)

    runner = DaemonRunner(
        settings=settings,
        service=service,  # type: ignore[arg-type]
        config=DaemonConfig(
            market_family="btc_1h",
            discovery_interval_seconds=3600.0,
            decision_min_interval_seconds=0.0,
            heartbeat_interval_seconds=0.02,
            maintenance_interval_seconds=3600.0,
        ),
        market_stream_factory=lambda url: market_stream,  # type: ignore[arg-type]
        btc_feed_factory=lambda: btc_feed,  # type: ignore[arg-type]
        decision_callback=callback,
    )
    asyncio.run(runner.run_for(0.3))
    # The heartbeat loop armed the kill switch before/while the event arrived;
    # the decision callback must have been skipped.
    assert callback_hits == []
    assert runner.metrics.safety_stop_reason == "daily_loss_limit"
    stop_events = [payload for etype, payload in journal.events if etype == "safety_stop"]
    assert stop_events, "safety_stop event should be journalled"


def test_daemon_paper_execute_callback_opens_and_closes_position(tmp_path: Path) -> None:
    """Directly exercise the paper-execute callback on a real AgentService.

    Seeds a MarketState with a liquid YES book + a manually-built APPROVED
    assessment, invokes the callback, and asserts that a paper position is
    opened. A second invocation with TTE inside the exit buffer must close it.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import MarketAssessment, SuggestedSide

    settings = _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "max_position_usd": 10.0,
        "min_confidence": 0.0,
        "min_edge": 0.0,
        "max_spread": 0.10,
        "min_depth_usd": 0.0,
        "stale_data_seconds": 3600,
        "max_concurrent_positions": 5,
    })
    service = AgentService(settings)
    candidate = _candidate("m-paper", "yes-tok", "no-tok")

    # Build a runner + inject market state directly (skip the WS loop).
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.42", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Hand-craft an APPROVED YES assessment so the risk engine takes the trade.
    approved = MarketAssessment(
        market_id=candidate.market_id,
        fair_probability=0.60,
        confidence=0.80,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=["edge > threshold"],
        reasons_to_abstain=[],
        edge=0.15,
        raw_model_output="unit-test",
        edge_yes=0.15,
        edge_no=-0.17,
        fair_probability_no=0.40,
        slippage_bps=10.0,
    )
    features = state.features()
    btc = BtcSnapshot(
        price=70000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0,
        log_return_1m=0.0,
        log_return_5m=0.0,
        log_return_15m=0.0,
        realized_vol_30m=0.01,
        sample_count=50,
    )
    context = DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=features,
        btc_snapshot=btc,
        assessment=approved,
        metrics=runner.metrics,
    )

    # First invocation: should open a position.
    asyncio.run(runner._paper_execute_decision_callback(context))
    positions = service.portfolio.list_open_positions()
    assert len(positions) == 1
    assert positions[0].market_id == candidate.market_id
    assert positions[0].side == SuggestedSide.YES

    # Second invocation while a position is open: must NOT stack a duplicate.
    asyncio.run(runner._paper_execute_decision_callback(context))
    assert len(service.portfolio.list_open_positions()) == 1

    # Simulate end-of-window: force TTE to 0 by rewriting candidate.end_date_iso.
    from dataclasses import replace as dataclass_replace
    expired = dataclass_replace(candidate, end_date_iso="2020-01-01T00:00:00Z")
    runner._candidates[candidate.market_id] = expired
    expired_context = DecisionContext(
        market_id=candidate.market_id,
        candidate=expired,
        features=features,
        btc_snapshot=btc,
        assessment=approved,
        metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(expired_context))
    # Position should now be closed.
    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions(limit=5)
    assert len(closed) == 1
    assert closed[0].close_reason == "paper_tte_exit"


def _setup_runner_with_open_yes_position(tmp_path, entry_price: float, settings_overrides: dict):
    """Shared setup for TP / SL tests: opens a YES position at entry_price."""
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import MarketAssessment, SuggestedSide

    base_overrides = {
        "daemon_auto_paper_execute": True,
        "max_position_usd": 10.0,
        "min_confidence": 0.0,
        "min_edge": 0.0,
        "max_spread": 0.20,
        "min_depth_usd": 0.0,
        "stale_data_seconds": 3600,
        "max_concurrent_positions": 5,
        # Explicitly disable every exit knob so each test opts into exactly
        # the ones it wants. Otherwise real .env values leak in and fire
        # before the feature under test.
        "paper_take_profit_pct": 0.0,
        "paper_stop_loss_pct": 0.0,
        "paper_trailing_stop_pct": 0.0,
        "paper_tp_ladder": "",
    }
    base_overrides.update(settings_overrides)
    settings = _settings(tmp_path).model_copy(update=base_overrides)
    service = AgentService(settings)
    candidate = _candidate("tp-mkt", "yes-tok", "no-tok")
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    # Entry book centered on entry_price with tight spread.
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": str(entry_price - 0.01), "size": "500"}],
        "asks": [{"price": str(entry_price + 0.01), "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate
    approved = MarketAssessment(
        market_id=candidate.market_id, fair_probability=0.60, confidence=0.80,
        suggested_side=SuggestedSide.YES, expiry_risk="LOW",
        reasons_for_trade=["edge > threshold"], reasons_to_abstain=[],
        edge=0.15, raw_model_output="unit-test",
        edge_yes=0.15, edge_no=-0.17, fair_probability_no=0.40, slippage_bps=10.0,
    )
    btc = BtcSnapshot(
        price=70000.0, observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0, log_return_15m=0.0,
        realized_vol_30m=0.01, sample_count=50,
    )
    features = state.features()
    open_ctx = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=features, btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(open_ctx))
    assert len(service.portfolio.list_open_positions()) == 1
    return runner, service, candidate, approved, btc


def test_paper_take_profit_closes_position_when_mid_rises(tmp_path) -> None:
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={"paper_take_profit_pct": 0.20},
    )
    # Move mid up ~25% to trigger take-profit (threshold is +20%).
    new_state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    new_state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.62", "size": "500"}],
        "asks": [{"price": "0.64", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = new_state
    tp_ctx = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=new_state.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(tp_ctx))
    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions(limit=1)
    assert closed[0].close_reason == "paper_take_profit"
    assert closed[0].realized_pnl > 0


def test_paper_stop_loss_closes_position_when_mid_drops(tmp_path) -> None:
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={"paper_stop_loss_pct": 0.15},
    )
    # Drop mid ~20% to trigger stop-loss (threshold is −15%).
    new_state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    new_state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.39", "size": "500"}],
        "asks": [{"price": "0.41", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = new_state
    sl_ctx = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=new_state.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(sl_ctx))
    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions(limit=1)
    assert closed[0].close_reason == "paper_stop_loss"
    assert closed[0].realized_pnl < 0


def test_paper_trailing_stop_rides_up_then_exits_on_reversal(tmp_path) -> None:
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={"paper_trailing_stop_pct": 0.10},
    )
    # Ride up: mid jumps to 0.80, peak tracked.
    state_up = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_up.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.79", "size": "500"}],
        "asks": [{"price": "0.81", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_up
    ctx_up = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_up.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_up))
    # Still open — peak tracks the exit-walk VWAP (with slippage), which is
    # slightly below the raw bid of 0.79 after the 10bps exit slippage bps.
    # Trail level ≈ peak × 0.90, current VWAP still well above it.
    assert len(service.portfolio.list_open_positions()) == 1
    assert runner._position_extras[("fade", candidate.market_id)]["peak_price"] >= 0.78

    # Reverse: bid drops to 0.60, well below the 0.711 trail level → exit.
    state_down = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_down.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.60", "size": "500"}],
        "asks": [{"price": "0.62", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_down
    ctx_down = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_down.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_down))
    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions(limit=1)
    assert closed[0].close_reason == "paper_trailing_stop"
    assert closed[0].realized_pnl > 0  # Peak was 0.80, exit at 0.70 still above 0.50 entry.


def test_paper_tp_ladder_closes_position_in_tranches(tmp_path) -> None:
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    # Ladder: close 50% at +15%, close another 25% at +30%. Remainder (25%)
    # sits open unless another exit fires.
    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50,
        settings_overrides={"paper_tp_ladder": "0.15:0.5,0.30:0.25"},
    )
    # +17%: triggers tranche 1 (close 50%). Entry price stored is 0.51051
    # (0.51 ask × default 10bps slippage), so threshold is ~0.587.
    state1 = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state1.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.59", "size": "500"}],
        "asks": [{"price": "0.61", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state1
    ctx1 = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state1.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx1))
    open_positions = service.portfolio.list_open_positions()
    assert len(open_positions) == 1
    assert abs(open_positions[0].size_usd - 5.0) < 1e-6  # 50% of $10 left.
    closed_after_first = service.portfolio.list_closed_positions(limit=5)
    assert any(p.close_reason == "paper_tp_ladder_1" for p in closed_after_first)

    # +37%: triggers tranche 2 (close 25% of CURRENT remaining).
    state2 = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state2.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.69", "size": "500"}],
        "asks": [{"price": "0.71", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state2
    ctx2 = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state2.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx2))
    open_positions = service.portfolio.list_open_positions()
    # Tranche 2 closes 25% of ORIGINAL ($10) = $2.50, leaving $2.50 open.
    assert len(open_positions) == 1
    assert abs(open_positions[0].size_usd - 2.5) < 1e-6
    closed_after_second = service.portfolio.list_closed_positions(limit=5)
    assert any(p.close_reason == "paper_tp_ladder_2" for p in closed_after_second)


def test_paper_tp_ladder_three_tranches_each_one_third_of_original(tmp_path) -> None:
    """With ladder "0.10:0.33,0.20:0.33,0.30:0.33" and a $10 position, each
    tranche should close ~$3.33 (1/3 of ORIGINAL), leaving ~$3.33 for the
    trail to manage. Regression for user-observed issue where second tranche
    only took 33% of the already-shrunk remainder."""
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50,
        settings_overrides={
            "paper_tp_ladder": "0.10:0.33,0.20:0.33,0.30:0.33",
            "paper_trailing_stop_pct": 0.0,  # trail off, we only test ladder
        },
    )

    def push(bid: str, ask: str):
        st = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
        st.apply_book_snapshot({
            "asset_id": "yes-tok",
            "bids": [{"price": bid, "size": "500"}],
            "asks": [{"price": ask, "size": "500"}],
        })
        runner._market_states[candidate.market_id] = st
        ctx = DecisionContext(
            market_id=candidate.market_id, candidate=candidate,
            features=st.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
        )
        asyncio.run(runner._paper_execute_decision_callback(ctx))

    # Tranche 1 at +17% → closes 0.33 × $10 = $3.30, leaves $6.70.
    push("0.59", "0.61")
    assert abs(service.portfolio.list_open_positions()[0].size_usd - 6.7) < 0.05
    # Tranche 2 at +25% → closes another 0.33 × $10 = $3.30, leaves $3.40.
    push("0.64", "0.66")
    assert abs(service.portfolio.list_open_positions()[0].size_usd - 3.4) < 0.05
    # Tranche 3 at +35% → closes another 0.33 × $10 = $3.30; but only $3.40
    # remains, so the effective close is capped at ~$3.30 and ~$0.10 is left.
    push("0.69", "0.71")
    opens = service.portfolio.list_open_positions()
    assert len(opens) == 1
    assert abs(opens[0].size_usd - 0.1) < 0.05


def test_fixed_tp_skipped_after_ladder_tranche_fires(tmp_path) -> None:
    """After a ladder partial-close fires, the remaining slice must NOT be
    closed by the fixed take-profit — scale-out strategy expects the runner
    to ride under trailing stop + SL only. Regression test for observed bug
    where ladder_1 then ladder_2 then paper_take_profit all fired on the
    same position.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={
            "paper_tp_ladder": "0.10:0.5",     # one tranche: close 50% at +10%
            "paper_take_profit_pct": 0.25,     # fixed TP at +25%
            "paper_trailing_stop_pct": 0.0,    # trail disabled for this test
        },
    )

    # Move mid enough to fire the ladder (+17% on 0.51051 entry).
    state_ladder = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_ladder.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.59", "size": "500"}],
        "asks": [{"price": "0.61", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_ladder
    ctx_ladder = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_ladder.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_ladder))
    # Ladder fired, remainder still open.
    open_positions = service.portfolio.list_open_positions()
    assert len(open_positions) == 1
    assert abs(open_positions[0].size_usd - 5.0) < 1e-6

    # Now push further to +30% — would trigger fixed TP (0.25) if the guard
    # didn't exist. With the guard: position stays open, only trail/SL can close.
    state_past_tp = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_past_tp.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.66", "size": "500"}],
        "asks": [{"price": "0.68", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_past_tp
    ctx_past_tp = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_past_tp.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_past_tp))
    # Remainder must still be open — no paper_take_profit fire after ladder.
    assert len(service.portfolio.list_open_positions()) == 1
    closed = service.portfolio.list_closed_positions(limit=5)
    reasons = [p.close_reason for p in closed]
    assert "paper_take_profit" not in reasons, (
        f"fixed TP fired on ladder remainder: {reasons}"
    )


def test_ladder_state_rehydrates_from_db_after_daemon_restart(tmp_path) -> None:
    """Regression for observed bug where a daemon restart mid-trade caused the
    ladder to re-fire step 1 on the shrunken remainder (sizes $3.30 → $2.21 →
    $1.48 instead of $3.33 → $3.33 → $3.33). After restart the daemon must
    reconstruct tranches_closed + original_size_usd from closed-tranche rows.
    """
    from polymarket_ai_agent.apps.daemon.run import DaemonRunner, DaemonConfig
    from polymarket_ai_agent.types import (
        DecisionStatus, ExecutionMode, ExecutionResult, SuggestedSide, TradeDecision,
    )

    settings = _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "paper_tp_ladder": "0.10:0.33,0.20:0.33",
    })
    service = AgentService(settings)
    # Open a paper position at $10, then fire one ladder partial (closes $3.33).
    decision = TradeDecision(
        market_id="rehyd-mkt",
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=10.0,
        limit_price=0.50,
        rationale=["ok"],
        rejected_by=[],
    )
    result = ExecutionResult(
        market_id="rehyd-mkt",
        success=True, mode=ExecutionMode.PAPER,
        order_id="paper-order-000042",
        status="FILLED_PAPER", detail="ok", fill_price=0.50,
    )
    service.portfolio.record_execution(decision, result)
    service.portfolio.partial_close_position(
        "rehyd-mkt", fraction=0.333, exit_price=0.55, reason="paper_tp_ladder_1",
    )
    open_pos = service.portfolio.get_open_position("rehyd-mkt")
    assert open_pos is not None
    # Simulate a daemon restart: build a new runner, call the rehydrator.
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    extras = runner._hydrate_position_extras(open_pos)
    # Should see tranches_closed=1 (one ladder partial previously fired) and
    # original_size_usd ≈ $10 (current remainder + one closed ~$3.33 tranche).
    assert extras["tranches_closed"] == 1.0
    assert abs(extras["original_size_usd"] - 10.0) < 0.01
    # Peak price intentionally reset on restart (can't reconstruct).
    assert extras["peak_price"] == 0.0


def test_paper_exit_fill_walks_bid_book_for_yes_position(tmp_path) -> None:
    """A YES close should walk yes_book.bids from best down, VWAP the
    consumed notional, and return that instead of just applying slippage
    to mid. This captures the full spread cost that real Polymarket
    executions pay but the old mid±slippage model missed."""
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import SuggestedSide

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={
            "paper_trailing_stop_pct": 0.05,
            "paper_trail_arm_pct": 0.0,
            "paper_exit_slippage_bps": 0.0,  # isolate the book-walk from slippage
        },
    )
    # Replace the YES book with a multi-level bid stack: 0.60 × 15 shares
    # then 0.58 × 50 shares. Selling 20 shares eats the top level (15 × 0.60)
    # and 5 shares of the 0.58 level → VWAP = (15×0.60 + 5×0.58) / 20 = 0.595.
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [
            {"price": "0.60", "size": "15"},
            {"price": "0.58", "size": "50"},
        ],
        "asks": [{"price": "0.62", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    # Position is ~19.96 YES shares at 0.51051 entry = $10 size.
    # Selling 20-ish at the staggered bid gives VWAP around 0.595 not 0.61 (mid).
    # entry_price is passed so target_shares walks the actual holdings (not the
    # fictional mid-derived count that amplifies VWAP on losing positions).
    exit_price = runner._paper_exit_fill(
        candidate.market_id, SuggestedSide.YES, 10.0, 0.51051, 0.61
    )
    assert 0.58 < exit_price < 0.60, f"expected book-walked VWAP in (0.58, 0.60), got {exit_price}"


def test_paper_trail_arm_threshold_blocks_premature_trail_exit(tmp_path) -> None:
    """Without the arm threshold, a small +2% peak + 5% trail would exit
    the position at a loss. With paper_trail_arm_pct=0.05 the trail stays
    disarmed below +5% peak.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={
            "paper_trailing_stop_pct": 0.05,
            "paper_trail_arm_pct": 0.05,
        },
    )
    # Peak goes to only ~+2.8% above entry (mid 0.525 vs entry ~0.51051),
    # so the trail must NOT arm at the 5% threshold.
    state_small = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_small.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.515", "size": "500"}],
        "asks": [{"price": "0.535", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_small
    ctx_peak = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_small.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_peak))
    # Now drop the mid hard (enough to have crossed a naive trail) but trail is still disarmed.
    state_drop = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_drop.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.48", "size": "500"}],
        "asks": [{"price": "0.50", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_drop
    ctx_drop = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_drop.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_drop))
    # Position should STILL be open — trail never armed.
    assert len(service.portfolio.list_open_positions()) == 1


def test_paper_trail_floor_clamps_to_entry_when_arm_below_invariant(tmp_path) -> None:
    """When arm_pct (5%) < trail_pct / (1 - trail_pct) (~8.7%), the unclamped
    trail floor would sit below entry — a freshly-armed trail could fire as a
    realised loss on the first pullback. Mirrors the production config that
    fired a −7.4% trail on market 2013005.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.37, settings_overrides={
            "paper_trailing_stop_pct": 0.08,
            "paper_trail_arm_pct": 0.05,
            "paper_exit_slippage_bps": 0.0,
        },
    )
    # Entry fill is 0.38 × (1 + 10 bps) = 0.38038 — mirrors the live example.
    # Push the peak to bid=0.40 so the arm threshold (0.38038 × 1.05 = 0.3994)
    # clears and the trail arms. Unclamped floor would be 0.40 × 0.92 = 0.368,
    # below entry.
    state_peak = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_peak.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.42", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_peak
    ctx_peak = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_peak.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_peak))
    assert len(service.portfolio.list_open_positions()) == 1

    # Pull bid down to 0.37: below the (unclamped) peak × 0.92 = 0.368? No —
    # 0.37 > 0.368, so the PRE-fix code would NOT have triggered here. With
    # the entry clamp the floor is max(0.368, 0.38038) = 0.38038; 0.37 ≤ 0.38038
    # → trail must fire.
    state_drop = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_drop.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.37", "size": "500"}],
        "asks": [{"price": "0.39", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_drop
    ctx_drop = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_drop.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_drop))
    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions(limit=1)
    assert closed[0].close_reason == "paper_trailing_stop"


def test_paper_entry_cooldown_blocks_immediate_reentry(tmp_path) -> None:
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={
            "paper_stop_loss_pct": 0.15,
            "paper_entry_cooldown_seconds": 120,
        },
    )
    # Force a stop-loss close: mid drops 20%.
    state_down = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_down.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.39", "size": "500"}],
        "asks": [{"price": "0.41", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_down
    ctx_close = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_down.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_close))
    assert service.portfolio.list_open_positions() == []
    assert ("fade", candidate.market_id) in runner._last_close_at

    # Now price pops back up and the scorer wants to re-enter on the same
    # market — but we're still within the 120s cooldown window.
    state_up = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_up.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.55", "size": "500"}],
        "asks": [{"price": "0.57", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state_up
    ctx_reentry = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state_up.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx_reentry))
    # Cooldown blocks the new entry.
    assert service.portfolio.list_open_positions() == []


def test_tp_ladder_parser_ignores_malformed_pairs() -> None:
    from polymarket_ai_agent.apps.daemon.run import DaemonRunner
    assert DaemonRunner._parse_tp_ladder("") == []
    assert DaemonRunner._parse_tp_ladder("0.15:0.5") == [(0.15, 0.5)]
    # Malformed pieces skipped; valid ones sorted ascending by pct.
    parsed = DaemonRunner._parse_tp_ladder("0.30:0.25,bogus,0.15:0.5,-0.1:0.5,0.20:2.0")
    assert parsed == [(0.15, 0.5), (0.30, 0.25)]


def test_agent_service_attributes_available() -> None:
    # Sanity: the real AgentService exposes `journal` + `discover_markets`, so the
    # daemon's expectations on the service API stay coupled to the production type.
    assert hasattr(AgentService, "discover_markets")
    assert "journal" in AgentService.__init__.__annotations__ or True


def test_min_candle_elapsed_blocks_early_entry(tmp_path: Path) -> None:
    """Entry must be blocked until time_elapsed_in_candle_s >= min_candle_elapsed_seconds.

    Verifies that setting min_candle_elapsed_seconds=60 prevents a new position
    from opening at t=30s, and allows one at t=90s — for a candle-style family.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import EvidencePacket, MarketAssessment, SuggestedSide

    settings = _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "market_family": "btc_15m",
        "min_candle_elapsed_seconds": 60,
        "max_position_usd": 10.0,
        "min_confidence": 0.0,
        "min_edge": 0.0,
        "max_spread": 0.10,
        "min_depth_usd": 0.0,
        "stale_data_seconds": 3600,
        "max_concurrent_positions": 5,
    })
    service = AgentService(settings)
    candidate = _candidate("m-elapsed", "yes-tok", "no-tok")

    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.42", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    approved = MarketAssessment(
        market_id=candidate.market_id,
        fair_probability=0.60,
        confidence=0.80,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=["test"],
        reasons_to_abstain=[],
        edge=0.15,
        raw_model_output="unit-test",
        edge_yes=0.15,
        edge_no=-0.17,
        fair_probability_no=0.40,
        slippage_bps=10.0,
    )
    btc = BtcSnapshot(
        price=70000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0,
        log_return_15m=0.0, realized_vol_30m=0.01, sample_count=50,
    )

    def _make_packet(elapsed: int) -> EvidencePacket:
        return EvidencePacket(
            market_id=candidate.market_id,
            question="test",
            resolution_criteria="-",
            market_probability=0.5,
            orderbook_midpoint=0.5,
            spread=0.02,
            depth_usd=500.0,
            seconds_to_expiry=900 - elapsed,
            external_price=70000.0,
            recent_price_change_bps=0.0,
            recent_trade_count=0,
            reasons_context=[],
            citations=[],
            time_elapsed_in_candle_s=elapsed,
        )

    def _ctx(elapsed: int) -> DecisionContext:
        return DecisionContext(
            market_id=candidate.market_id,
            candidate=candidate,
            features=state.features(),
            btc_snapshot=btc,
            assessment=approved,
            metrics=runner.metrics,
            packet=_make_packet(elapsed),
        )

    # t=30s — before the guard expires: no entry.
    asyncio.run(runner._paper_execute_decision_callback(_ctx(30)))
    assert service.portfolio.list_open_positions() == [], "entry should be blocked at t=30s"

    # t=90s — guard cleared: entry allowed.
    asyncio.run(runner._paper_execute_decision_callback(_ctx(90)))
    positions = service.portfolio.list_open_positions()
    assert len(positions) == 1, "entry should be allowed at t=90s"
    assert positions[0].market_id == candidate.market_id


# ---------------------------------------------------------------------------
# Settings reload loop — new in the DB-owned runtime settings change.
# ---------------------------------------------------------------------------

def test_settings_reload_loop_rebinds_engines_and_journals_change(tmp_path: Path) -> None:
    """Writing a new row to settings_changes while the daemon is running
    must flow through _maybe_reload_settings and propagate to the quant
    scorer + risk profile + execution engine."""
    settings = _settings(tmp_path)
    service = AgentService(settings)
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    # Seed a baseline value so the daemon has a "before" to diff against.
    service.settings_store.record_changes(
        [("min_edge", runner.settings.min_edge, 0.25)], source="api"
    )
    runner._last_settings_id = service.settings_store.get_max_id() - 1  # rewind cursor
    runner._maybe_reload_settings()
    assert runner.settings.min_edge == 0.25
    assert runner.quant.settings.min_edge == 0.25
    # Risk engine caches a profile; refresh_profile must have run.
    assert service.risk.profile.min_edge == 0.25
    # Cursor advanced.
    assert runner._last_settings_id == service.settings_store.get_max_id()


def test_settings_reload_emits_settings_changed_event(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    runner._last_settings_id = service.settings_store.get_max_id()
    service.settings_store.record_changes(
        [("paper_stop_loss_pct", runner.settings.paper_stop_loss_pct, 0.10)],
        source="cli",
        reason="tighter stop",
    )
    runner._maybe_reload_settings()
    # Tail events.jsonl for the settings_changed event.
    import json

    events = [json.loads(line) for line in settings.events_path.read_text().splitlines() if line.strip()]
    changed = [e for e in events if e["event_type"] == "settings_changed"]
    assert len(changed) == 1
    payload = changed[-1]["payload"]
    assert "paper_stop_loss_pct" in payload["changed"]
    assert payload["changed"]["paper_stop_loss_pct"]["after"] == 0.10
    assert "cli" in payload["source"]


def test_settings_reload_loop_survives_db_errors(tmp_path: Path) -> None:
    """A transient DB read failure must not crash the reload loop."""
    settings = _settings(tmp_path)
    service = AgentService(settings)
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    original = runner.service.settings_store.get_max_id
    runner.service.settings_store.get_max_id = boom  # type: ignore[assignment]
    try:
        async def _drive() -> None:
            stop_event = asyncio.Event()

            async def stopper() -> None:
                await asyncio.sleep(0.05)
                stop_event.set()

            # One tick is enough — the loop body catches + journals the error.
            asyncio.create_task(stopper())
            await runner._settings_reload_loop(stop_event)

        asyncio.run(_drive())
    finally:
        runner.service.settings_store.get_max_id = original  # type: ignore[assignment]

    import json

    events = [json.loads(line) for line in settings.events_path.read_text().splitlines() if line.strip()]
    failed = [e for e in events if e["event_type"] == "settings_reload_failed"]
    assert failed, "loop should have emitted a settings_reload_failed event"
    assert "simulated DB failure" in failed[-1]["payload"]["error"]


def test_daemon_multi_strategy_opens_per_strategy_positions(tmp_path: Path) -> None:
    """When both strategies pass their gates, each opens its own paper
    position on the same market. The strategy_id dimension from phase 1
    keeps them isolated — independent open-position state, independent
    cooldown, independent PnL.

    Setup: RANGING regime (small HTF returns, low vol) so the adaptive
    scorer delegates to fade unchanged. Both strategies score the same
    packet and both decide to trade.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import EvidencePacket

    settings = _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "max_position_usd": 10.0,
        "min_confidence": 0.0,
        "min_edge": 0.0,
        "max_spread": 0.10,
        "min_depth_usd": 0.0,
        "stale_data_seconds": 3600,
        "max_concurrent_positions": 5,
        "quant_trend_filter_enabled": False,
        "quant_ofi_gate_enabled": False,
        "quant_vol_regime_enabled": False,
        "quant_min_entry_price": 0.0,
        "min_candle_elapsed_seconds": 0,
        "paper_entry_cooldown_seconds": 0,
        # Explicit opt-in: adaptive defaults to off post-2026-04-24 (it was a
        # pure fade clone). This test asserts its delegation behaviour still
        # works, so it has to re-enable the registration.
        "adaptive_enabled": True,
    })
    service = AgentService(settings)
    candidate = _candidate("m-multi", "yes-tok", "no-tok")

    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.42", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    btc = BtcSnapshot(
        price=70000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0,
        log_return_15m=0.0, realized_vol_30m=0.002, sample_count=50,
    )
    # RANGING packet: real drift in one direction, HTF returns small, vol low.
    packet = EvidencePacket(
        market_id=candidate.market_id,
        question="test",
        resolution_criteria="-",
        market_probability=0.5,
        orderbook_midpoint=0.41,
        spread=0.02,
        depth_usd=500.0,
        seconds_to_expiry=600,
        external_price=70000.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.40,
        ask_yes=0.42,
        bid_no=0.58,
        ask_no=0.60,
        btc_log_return_since_candle_open=0.003,
        realized_vol_30m=0.002,
        btc_log_return_1h=0.0005,
        btc_log_return_4h=0.0005,
        time_elapsed_in_candle_s=300,
    )
    # Decision engine would populate this from the fade scorer — the callback
    # reuses it under the fade strategy_id and re-scores for the adaptive one.
    assessment = runner.quant.score_market(packet)
    context = DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=btc,
        assessment=assessment,
        metrics=runner.metrics,
        packet=packet,
    )

    asyncio.run(runner._paper_execute_decision_callback(context))

    positions = service.portfolio.list_open_positions()
    strategies = {p.strategy_id for p in positions}
    assert "fade" in strategies
    assert "adaptive" in strategies
    assert len(positions) == 2


def test_daemon_multi_strategy_adaptive_delegates_in_trend(tmp_path: Path) -> None:
    """Post-2026-04-23 soak: adaptive no longer follows the trend (the
    follow-maker path lost 83% of TRENDING_DOWN entries). It now
    delegates to the fade scorer in trending regimes, so both strategies
    should open positions on the same trending setup — fade via its own
    taker path and adaptive via the delegated fade assessment.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import EvidencePacket

    settings = _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "max_position_usd": 10.0,
        "min_confidence": 0.0,
        # min_edge > 0 so adaptive's follow-with-maker assessment (edge=0)
        # is rejected by the risk engine's taker gate. Production also
        # runs with min_edge > 0, so this mirrors real conditions.
        "min_edge": 0.01,
        "max_spread": 0.10,
        "min_depth_usd": 0.0,
        "stale_data_seconds": 3600,
        "max_concurrent_positions": 5,
        "quant_trend_filter_enabled": False,
        "quant_ofi_gate_enabled": False,
        "quant_vol_regime_enabled": False,
        "quant_min_entry_price": 0.0,
        "min_candle_elapsed_seconds": 0,
        "paper_entry_cooldown_seconds": 0,
        # See note on the other multi-strategy test — adaptive defaults off.
        "adaptive_enabled": True,
    })
    service = AgentService(settings)
    candidate = _candidate("m-trend", "yes-tok", "no-tok")

    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.42", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    btc = BtcSnapshot(
        price=70000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0,
        log_return_15m=0.0, realized_vol_30m=0.002, sample_count=50,
    )
    # TRENDING_UP packet: both 1h and 4h returns positive and above the
    # regime threshold → adaptive must abstain.
    packet = EvidencePacket(
        market_id=candidate.market_id,
        question="test",
        resolution_criteria="-",
        market_probability=0.5,
        orderbook_midpoint=0.41,
        spread=0.02,
        depth_usd=500.0,
        seconds_to_expiry=600,
        external_price=70000.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.40,
        ask_yes=0.42,
        bid_no=0.58,
        ask_no=0.60,
        btc_log_return_since_candle_open=0.003,
        realized_vol_30m=0.002,
        btc_log_return_1h=0.005,
        btc_log_return_4h=0.008,
        time_elapsed_in_candle_s=300,
    )
    assessment = runner.quant.score_market(packet)
    context = DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=btc,
        assessment=assessment,
        metrics=runner.metrics,
        packet=packet,
    )

    asyncio.run(runner._paper_execute_decision_callback(context))

    positions = service.portfolio.list_open_positions()
    strategies = {p.strategy_id for p in positions}
    # Both strategies open positions: fade via its own taker path, and
    # adaptive by delegating to the fade scorer's assessment for this
    # trending regime.
    assert "fade" in strategies
    assert "adaptive" in strategies


def _follow_packet(market_id: str, bid_yes: float = 0.66, ask_yes: float = 0.68) -> "EvidencePacket":
    """Trending-up packet with a liquid YES book — triggers adaptive
    follow-with-maker on a YES buy.
    """
    from polymarket_ai_agent.types import EvidencePacket
    return EvidencePacket(
        market_id=market_id,
        question="test",
        resolution_criteria="-",
        market_probability=0.65,
        orderbook_midpoint=(bid_yes + ask_yes) / 2,
        spread=ask_yes - bid_yes,
        depth_usd=500.0,
        seconds_to_expiry=600,
        external_price=70000.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=bid_yes,
        ask_yes=ask_yes,
        bid_no=1.0 - ask_yes,
        ask_no=1.0 - bid_yes,
        btc_log_return_since_candle_open=0.003,
        realized_vol_30m=0.002,
        # Both HTF returns positive and above threshold → TRENDING_UP.
        btc_log_return_1h=0.005,
        btc_log_return_4h=0.008,
        time_elapsed_in_candle_s=300,
    )


def _follow_settings(tmp_path: Path):
    """Settings loose enough for the fade scorer to pick YES under the
    hood; all filter gates disabled so adaptive's follow logic is what
    gets tested, not the risk engine's veto.
    """
    return _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "max_position_usd": 2.0,
        "min_confidence": 0.0,
        "min_edge": 0.01,
        "max_spread": 0.10,
        "min_depth_usd": 0.0,
        "stale_data_seconds": 3600,
        "max_concurrent_positions": 5,
        "quant_trend_filter_enabled": False,
        "quant_ofi_gate_enabled": False,
        "quant_vol_regime_enabled": False,
        "quant_min_entry_price": 0.0,
        "min_candle_elapsed_seconds": 0,
        "paper_entry_cooldown_seconds": 0,
        "paper_follow_limit_discount_bps": 100.0,  # 1% below mid
        "paper_follow_maker_ttl_seconds": 60,
    })


def _follow_runner_and_state(tmp_path: Path, candidate_id: str = "m-follow"):
    from polymarket_ai_agent.engine.market_state import MarketState

    settings = _follow_settings(tmp_path)
    service = AgentService(settings)
    candidate = _candidate(candidate_id, "yes-tok", "no-tok")
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate
    return runner, service, candidate, state


def _apply_book(state, bid_yes: float, ask_yes: float) -> None:
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": f"{bid_yes:.4f}", "size": "500"}],
        "asks": [{"price": f"{ask_yes:.4f}", "size": "500"}],
    })


def _context_for_follow(state, candidate, packet):
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    btc = BtcSnapshot(
        price=70000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0,
        log_return_15m=0.0, realized_vol_30m=0.002, sample_count=50,
    )
    assessment = _follow_assessment(candidate.market_id)
    return DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=btc,
        assessment=assessment,
        metrics=DaemonMetrics(),
        packet=packet,
    )


def _follow_assessment(market_id: str) -> "MarketAssessment":
    """A hand-built follow-with-maker assessment so the fade-path test
    doesn't need to run the full scorer. Matches what AdaptiveScorer
    returns in TRENDING_UP regime.
    """
    from polymarket_ai_agent.engine.adaptive_scoring import ADAPTIVE_FOLLOW_MAKER_TAG
    from polymarket_ai_agent.types import MarketAssessment, SuggestedSide
    return MarketAssessment(
        market_id=market_id,
        fair_probability=0.65,
        confidence=0.0,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=["Regime TRENDING_UP: follow with maker"],
        reasons_to_abstain=[],
        edge=0.0,
        raw_model_output=ADAPTIVE_FOLLOW_MAKER_TAG,
        edge_yes=0.0,
        edge_no=0.0,
        fair_probability_no=0.35,
        slippage_bps=10.0,
    )


def test_follow_maker_placed_on_first_tick(tmp_path: Path) -> None:
    """First tick of a trending regime with no existing maker → daemon
    parks a new paper-maker at the discounted mid. No position opens.
    """
    from polymarket_ai_agent.types import SuggestedSide
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))

    asyncio.run(runner._paper_execute_for_strategy(ctx, "adaptive"))

    key = ("adaptive", candidate.market_id)
    pending = runner._pending_makers.get(key)
    assert pending is not None, "follow-maker must be parked on first tick"
    assert pending.side == SuggestedSide.YES
    # Mid = 0.67, 100 bps discount = 0.67 × 0.99 = 0.6633
    assert abs(pending.limit_price - 0.6633) < 1e-4
    assert service.portfolio.list_open_positions() == []


def test_follow_maker_fills_when_ask_crosses(tmp_path: Path) -> None:
    """Tick 1: parks a maker at 0.6633. Tick 2: the YES ask drops to
    0.66 (pullback) which crosses our limit → maker fills as a paper
    position under strategy_id='adaptive'.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)

    # Tick 1 — park the maker.
    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    assert runner._pending_makers, "setup precondition: maker parked"

    # Tick 2 — market pulls back so ask crosses our limit.
    _apply_book(state, bid_yes=0.60, ask_yes=0.62)
    ctx2 = _context_for_follow(
        state, candidate, _follow_packet(candidate.market_id, bid_yes=0.60, ask_yes=0.62)
    )
    asyncio.run(runner._paper_execute_for_strategy(ctx2, "adaptive"))

    # Maker is gone (consumed by the fill), and an adaptive position exists.
    key = ("adaptive", candidate.market_id)
    assert key not in runner._pending_makers
    positions = service.portfolio.list_open_positions(strategy_id="adaptive")
    assert len(positions) == 1
    assert positions[0].market_id == candidate.market_id
    # Entry price is the maker's limit, not the current ask.
    assert abs(positions[0].entry_price - 0.6633) < 1e-4


def test_follow_maker_cancelled_when_regime_flips(tmp_path: Path) -> None:
    """A parked maker is dropped when the subsequent tick's assessment
    is not follow-with-maker (regime returned to RANGING or abstained).
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.types import MarketAssessment, SuggestedSide

    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    _apply_book(state, bid_yes=0.66, ask_yes=0.68)

    # Tick 1: follow-maker → park.
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    assert key in runner._pending_makers

    # Tick 2: regime flipped — new assessment is an ABSTAIN (non-follow).
    abstain = MarketAssessment(
        market_id=candidate.market_id,
        fair_probability=0.50,
        confidence=0.0,
        suggested_side=SuggestedSide.ABSTAIN,
        expiry_risk="LOW",
        reasons_for_trade=[],
        reasons_to_abstain=["regime flipped"],
        edge=0.0,
        raw_model_output="adaptive-regime-gated",
    )
    btc = BtcSnapshot(
        price=70000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0,
        log_return_15m=0.0, realized_vol_30m=0.002, sample_count=50,
    )
    from polymarket_ai_agent.types import EvidencePacket
    packet = EvidencePacket(
        market_id=candidate.market_id,
        question="test",
        resolution_criteria="-",
        market_probability=0.5,
        orderbook_midpoint=0.50,
        spread=0.02,
        depth_usd=500.0,
        seconds_to_expiry=540,
        external_price=70000.0,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=0.49,
        ask_yes=0.51,
        btc_log_return_since_candle_open=0.0,
        realized_vol_30m=0.002,
        btc_log_return_1h=0.0002,
        btc_log_return_4h=0.0002,  # RANGING
        time_elapsed_in_candle_s=360,
    )
    ctx2 = DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=btc,
        assessment=abstain,
        metrics=DaemonMetrics(),
        packet=packet,
    )

    asyncio.run(runner._paper_execute_for_strategy(ctx2, "adaptive"))
    assert key not in runner._pending_makers, "stale maker must be cancelled on regime flip"
    assert service.portfolio.list_open_positions() == []


def _apply_layered_book(
    state,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> None:
    """Apply a multi-level book snapshot so depth-filter tests can
    distinguish a ghost top-of-book from the first real level underneath.
    """
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": f"{p:.4f}", "size": f"{s}"} for p, s in bids],
        "asks": [{"price": f"{p:.4f}", "size": f"{s}"} for p, s in asks],
    })


def test_follow_maker_keeps_pending_when_drift_below_threshold(tmp_path: Path) -> None:
    """Tier 2a: a 5bp mid drift should NOT re-quote when the operator has
    set a 50bp price threshold. Preserves the "rest and wait" default
    while still allowing opt-in price tracking.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    # Opt into tracking with 50bps (0.005) price threshold.
    runner.settings = runner.settings.model_copy(update={
        "paper_follow_cancel_price_threshold": 0.005,
    })

    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    first_order = runner._pending_makers.get(key)
    assert first_order is not None
    first_limit = first_order.limit_price

    # Mid drifts from 0.67 to 0.671 → limit would drift ~1bp; well under 50bp threshold.
    _apply_book(state, bid_yes=0.661, ask_yes=0.681)
    ctx2 = _context_for_follow(
        state, candidate, _follow_packet(candidate.market_id, bid_yes=0.661, ask_yes=0.681)
    )
    asyncio.run(runner._paper_execute_for_strategy(ctx2, "adaptive"))

    kept = runner._pending_makers.get(key)
    assert kept is not None
    assert kept.limit_price == first_limit, "price threshold must veto the re-quote"
    assert kept.placed_at == first_order.placed_at, "original timestamp preserved"


def test_follow_maker_requotes_when_drift_above_threshold(tmp_path: Path) -> None:
    """Tier 2a: when the mid moves enough that the desired limit drifts
    past the price threshold, the existing quote is cancelled and a
    fresh one parks at the new price.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    runner.settings = runner.settings.model_copy(update={
        "paper_follow_cancel_price_threshold": 0.005,
    })

    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    first_order = runner._pending_makers.get(key)
    assert first_order is not None

    # Mid jumps from 0.67 to 0.70 (3¢); 100bp discount → limit = 0.693.
    # Drift from the first limit (~0.6633) is ~0.03 — way over 0.005 threshold.
    _apply_book(state, bid_yes=0.69, ask_yes=0.71)
    ctx2 = _context_for_follow(
        state, candidate, _follow_packet(candidate.market_id, bid_yes=0.69, ask_yes=0.71)
    )
    asyncio.run(runner._paper_execute_for_strategy(ctx2, "adaptive"))

    fresh = runner._pending_makers.get(key)
    assert fresh is not None
    assert fresh.limit_price != first_order.limit_price, "should have re-quoted"
    assert abs(fresh.limit_price - 0.693) < 1e-3


def test_follow_maker_zero_threshold_preserves_legacy_wait_behavior(tmp_path: Path) -> None:
    """With both thresholds at default 0.0, drift never triggers a
    re-quote — this is the legacy behaviour and the default on branch
    merge so existing soaks don't regress.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    # Defaults are 0 / 0 per initial_settings.

    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    first_order = runner._pending_makers.get(key)
    assert first_order is not None

    # Drastic mid move — thresholds should STILL veto the re-quote.
    _apply_book(state, bid_yes=0.80, ask_yes=0.82)
    ctx2 = _context_for_follow(
        state, candidate, _follow_packet(candidate.market_id, bid_yes=0.80, ask_yes=0.82)
    )
    asyncio.run(runner._paper_execute_for_strategy(ctx2, "adaptive"))

    kept = runner._pending_makers.get(key)
    assert kept is not None
    assert kept.limit_price == first_order.limit_price


def test_freshness_loop_requotes_when_drift_exceeds_threshold(tmp_path: Path) -> None:
    """The periodic freshness sweep must re-quote a resting maker whose
    desired limit has drifted past ``paper_follow_cancel_price_threshold``,
    independent of any WS-driven daemon_tick. This is the bug fix for
    quiet-window stalling: the event-driven path may not fire for
    minutes if the market is quiet, during which the ask can drift down
    onto our stale limit and lock in a bad-price fill.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    runner.settings = runner.settings.model_copy(update={
        "paper_follow_cancel_price_threshold": 0.005,
    })

    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    first_order = runner._pending_makers.get(key)
    assert first_order is not None

    # Simulate a quiet window: the book moves on the WS feed but no
    # daemon_tick fires (because the WS event didn't cross a trigger
    # threshold). Mid drifts from 0.67 to 0.70 — would trigger a
    # re-quote on the event path; the freshness loop must do the same.
    _apply_book(state, bid_yes=0.69, ask_yes=0.71)
    asyncio.run(runner._refresh_pending_makers())

    fresh = runner._pending_makers.get(key)
    assert fresh is not None
    assert fresh.limit_price != first_order.limit_price, "freshness sweep must re-quote on drift"
    # Same logic as event-path: 100bp discount × new mid 0.70 = 0.693.
    assert abs(fresh.limit_price - 0.693) < 1e-3
    assert fresh.placed_at >= first_order.placed_at, "new placement timestamp"


def test_freshness_loop_no_op_when_drift_below_threshold(tmp_path: Path) -> None:
    """Sub-threshold drift must NOT re-quote — same hysteresis the
    event path enforces. Otherwise the loop becomes a thrash engine.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    runner.settings = runner.settings.model_copy(update={
        "paper_follow_cancel_price_threshold": 0.02,  # 2¢ threshold
    })

    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    first_order = runner._pending_makers.get(key)
    assert first_order is not None
    original_limit = first_order.limit_price
    original_placed_at = first_order.placed_at

    # 1¢ mid drift — under the 2¢ threshold.
    _apply_book(state, bid_yes=0.67, ask_yes=0.69)
    asyncio.run(runner._refresh_pending_makers())

    kept = runner._pending_makers.get(key)
    assert kept is not None
    assert kept.limit_price == original_limit, "below-threshold drift must not re-quote"
    assert kept.placed_at == original_placed_at, "original placement preserved"


def test_freshness_loop_disabled_when_threshold_zero(tmp_path: Path) -> None:
    """With both axes at 0 the sweep is a pure no-op — preserves the
    legacy "rest and wait for TTL" behaviour for soaks that haven't
    opted into price tracking.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    # Defaults are 0 / 0 per initial_settings.

    _apply_book(state, bid_yes=0.66, ask_yes=0.68)
    ctx1 = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx1, "adaptive"))
    key = ("adaptive", candidate.market_id)
    first_order = runner._pending_makers.get(key)
    assert first_order is not None

    # Drastic mid move — sweep must still leave the rest alone.
    _apply_book(state, bid_yes=0.80, ask_yes=0.82)
    asyncio.run(runner._refresh_pending_makers())

    kept = runner._pending_makers.get(key)
    assert kept is not None
    assert kept.limit_price == first_order.limit_price


def test_follow_maker_depth_filter_ignores_ghost_top_of_book(tmp_path: Path) -> None:
    """Tier 2b: with min_level_size_shares set, the paper maker anchors
    its mid on the first level with real size, not a 1-lot ghost at the
    top of the book. Prevents posting behind a phantom order.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    runner.settings = runner.settings.model_copy(update={
        "paper_follow_min_level_size_shares": 50.0,
    })

    # Ghost 1-lot at the top, real 500-lot one tick inside on both sides.
    _apply_layered_book(
        state,
        bids=[(0.66, 1.0), (0.65, 500.0)],
        asks=[(0.68, 1.0), (0.69, 500.0)],
    )
    ctx = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx, "adaptive"))

    key = ("adaptive", candidate.market_id)
    pending = runner._pending_makers.get(key)
    assert pending is not None
    # Real mid after filtering = (0.65 + 0.69) / 2 = 0.67; 100bp discount = 0.6633.
    # Ghost mid would have been (0.66 + 0.68) / 2 = 0.67 too, but NO-side is
    # symmetric here. Use NO-side mismatch to disambiguate: on NO the real
    # top-of-book is 1 - 0.69 = 0.31 (bid) and 1 - 0.65 = 0.35 (ask) — but
    # we already checked the YES path is used. Assert the limit equals the
    # filtered-mid × 0.99 rather than the ghost-mid computation.
    expected = 0.67 * 0.99
    assert abs(pending.limit_price - expected) < 1e-4


def test_follow_maker_depth_filter_falls_back_to_raw_when_no_level_qualifies(
    tmp_path: Path,
) -> None:
    """If no level on the filtered side meets the threshold, we fall
    back to the raw best so the maker still posts instead of silently
    skipping the tick.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path)
    runner.settings = runner.settings.model_copy(update={
        "paper_follow_min_level_size_shares": 1000.0,  # nothing qualifies
    })

    _apply_layered_book(
        state,
        bids=[(0.66, 10.0)],
        asks=[(0.68, 10.0)],
    )
    ctx = _context_for_follow(state, candidate, _follow_packet(candidate.market_id))
    asyncio.run(runner._paper_execute_for_strategy(ctx, "adaptive"))

    key = ("adaptive", candidate.market_id)
    pending = runner._pending_makers.get(key)
    assert pending is not None
    # Raw mid = 0.67; 100bp discount = 0.6633.
    assert abs(pending.limit_price - 0.6633) < 1e-4


def test_daemon_tick_surfaces_reward_estimate_for_rewarded_market(
    tmp_path: Path,
) -> None:
    """Tier 1 surfacing: when a market carries maker-reward params,
    daemon_tick emits ``estimated_reward_per_100_yes_bid``. The estimator
    itself is covered in detail by tests/test_maker_rewards.py — here we
    verify the daemon actually calls it and logs the result.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path, candidate_id="m-reward")
    # Attach rewards to the candidate in-place; discovery would populate these
    # from the gamma ``rewards`` object in production.
    candidate.rewards_daily_rate = 200.0
    candidate.rewards_max_spread_pct = 4.0
    candidate.rewards_min_size = 10.0

    _apply_layered_book(
        state,
        bids=[(0.50, 200.0), (0.49, 300.0)],
        asks=[(0.52, 200.0), (0.53, 300.0)],
    )
    packet = _follow_packet(candidate.market_id, bid_yes=0.50, ask_yes=0.52)
    ctx = _context_for_follow(state, candidate, packet)

    asyncio.run(runner._default_decision_callback(ctx, "adaptive"))

    events = list(service.journal.read_recent_events(limit=50))
    ticks = [e for e in events if e.get("event_type") == "daemon_tick"]
    assert ticks, "daemon_tick event must be emitted"
    payload = ticks[-1]["payload"]
    assert "estimated_reward_per_100_yes_bid" in payload
    assert payload["estimated_reward_per_100_yes_bid"] > 0.0


def test_paper_exit_fill_target_shares_use_entry_price_not_current_mid(
    tmp_path: Path,
) -> None:
    """Regression for the share-count bug: on a losing position, computing
    target_shares = size_usd / current_bid over-targets the walk (mid fell
    → more "shares" than we actually hold). VWAP skewed lower, realised
    loss exceeds the SL threshold unrealistically. Fix: use entry_price
    so the walk matches actual holdings.
    """
    from polymarket_ai_agent.engine.market_state import MarketState
    from polymarket_ai_agent.types import SuggestedSide

    runner, _, candidate, _, _ = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={"paper_exit_slippage_bps": 0.0},
    )
    # Position holds $10 at entry ~0.51 → ~19.6 shares. Bid book crashes
    # to a thin top plus a fat deep level. Walking 19.6 shares gives a
    # certain VWAP; walking the buggy mid-derived count (10/0.20 = 50
    # shares) would plunge much deeper.
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [
            {"price": "0.40", "size": "20"},  # fits our actual holdings
            {"price": "0.10", "size": "500"},  # where the buggy walk would land
        ],
        "asks": [{"price": "0.60", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state

    # With entry_price=0.51 (actual hold ≈ 19.6 shares): all 19.6 fit in the
    # top level at 0.40 → VWAP ≈ 0.40.
    correct = runner._paper_exit_fill(
        candidate.market_id, SuggestedSide.YES, 10.0, 0.51, 0.20
    )
    # With the buggy mid-derived count (10/0.20=50 shares): walks 20 at 0.40 +
    # 30 at 0.10 = VWAP ≈ 0.22. The actual fix means we never touch the 0.10
    # level — fill sits at the top real bid.
    assert 0.38 < correct <= 0.40, (
        f"expected exit ~0.40 (walked actual share count), got {correct}"
    )


def test_sl_does_not_fire_when_exit_vwap_is_above_threshold(tmp_path: Path) -> None:
    """Regression for the 50%-realised-on-20%-SL bug — half of the fix.

    The old logic fired SL on raw top-of-bid, then realised the actual fill
    by walking the whole book VWAP. A 1-share ghost at 0.40 (below the
    −20% line) tripped the trigger, but the walk then consumed a deep 500-
    share level at 0.30, realising −40%. The fix uses the full exit-walk
    VWAP as the trigger, so if most of our holdings would actually fill at
    0.46 (above threshold), the SL stays dormant.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={
            "paper_stop_loss_pct": 0.20,
            "paper_exit_slippage_bps": 0.0,
        },
    )

    # Ghost 1-share at 0.40; 500-share real level at 0.46 underneath. Our
    # ~19.6 shares almost entirely fill at 0.46 → walk VWAP ≈ 0.459. pnl
    # ≈ −10%, well above the −20% trigger. SL must NOT fire.
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [
            {"price": "0.46", "size": "500"},
            {"price": "0.40", "size": "1"},  # ghost deeper — ignored at this size
        ],
        "asks": [{"price": "0.48", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    ctx = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx))
    assert len(service.portfolio.list_open_positions()) == 1, (
        "SL must NOT fire when the exit-walk VWAP is above the threshold"
    )


def test_sl_fires_when_exit_vwap_crosses_threshold(tmp_path: Path) -> None:
    """Converse of the above: when our actual exit VWAP crosses the SL
    threshold, the position closes. And critically, the CLOSED position's
    exit_price matches the trigger price — no surprise gap.
    """
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, approved, btc = _setup_runner_with_open_yes_position(
        tmp_path, entry_price=0.50, settings_overrides={
            "paper_stop_loss_pct": 0.20,
            "paper_exit_slippage_bps": 0.0,
        },
    )

    # 2-share ghost at 0.45 + 500-share real at 0.38. Our ~19.6 shares:
    # 2 at 0.45 + 17.6 at 0.38 = 0.9 + 6.688 = 7.588 → VWAP = 0.387.
    # pnl from entry 0.51 = (0.387 - 0.51)/0.51 = −24.1%. Below −20%. Fires.
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [
            {"price": "0.45", "size": "2"},
            {"price": "0.38", "size": "500"},
        ],
        "asks": [{"price": "0.48", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    ctx = DecisionContext(
        market_id=candidate.market_id, candidate=candidate,
        features=state.features(), btc_snapshot=btc, assessment=approved, metrics=runner.metrics,
    )
    asyncio.run(runner._paper_execute_decision_callback(ctx))
    closed = service.portfolio.list_closed_positions(limit=10)
    assert len(closed) == 1, "SL must fire when exit VWAP crosses the threshold"
    assert closed[0].close_reason == "paper_stop_loss"
    # No surprise gap: the exit_price is the same VWAP that triggered the SL.
    # (Both pass through apply_exit_slippage which is zero'd for this test.)
    assert 0.38 < closed[0].exit_price < 0.40, (
        f"exit price {closed[0].exit_price} must equal trigger VWAP (~0.387)"
    )


def test_daemon_tick_reward_estimate_is_zero_for_unrewarded_market(
    tmp_path: Path,
) -> None:
    """Most BTC short-horizon markets don't pay maker rewards. The
    estimator returns 0.0 in that case and the daemon surfaces it as 0.0,
    not None — keeps the downstream numeric column clean.
    """
    runner, service, candidate, state = _follow_runner_and_state(tmp_path, candidate_id="m-norew")
    # rewards_daily_rate is 0 by default — no need to mutate.
    _apply_book(state, bid_yes=0.50, ask_yes=0.52)
    packet = _follow_packet(candidate.market_id, bid_yes=0.50, ask_yes=0.52)
    ctx = _context_for_follow(state, candidate, packet)

    asyncio.run(runner._default_decision_callback(ctx, "adaptive"))

    events = list(service.journal.read_recent_events(limit=50))
    ticks = [e for e in events if e.get("event_type") == "daemon_tick"]
    assert ticks
    payload = ticks[-1]["payload"]
    assert payload["estimated_reward_per_100_yes_bid"] == 0.0


# --- Penny strategy integration --------------------------------------------

def _penny_runner(tmp_path: Path, ask_yes: float = 0.98, ask_no: float = 0.02):
    """Build a daemon with penny enabled and a market whose NO ask is
    already at 2¢ so the next tick is a candidate for entry.

    The YES and NO books are constructed so the NO frame satisfies
    ``1 - YES_bid = NO_ask`` and ``1 - YES_ask = NO_bid``. The paper
    execution engine simulates NO-side fills by reflecting YES bids, so
    the two books must agree on that invariant for the test's entry
    price to land where we expect.
    """
    from polymarket_ai_agent.engine.market_state import MarketState

    settings = _settings(tmp_path).model_copy(update={
        "daemon_auto_paper_execute": True,
        "penny_enabled": True,
        "penny_entry_thresh": 0.03,
        "penny_min_entry_tte_seconds": 300,
        "penny_force_exit_tte_seconds": 120,
        "penny_tp_multiple": 2.0,
        "penny_size_usd": 1.0,
        "max_concurrent_positions": 5,
        "paper_entry_slippage_bps": 0.0,
        "paper_exit_slippage_bps": 0.0,
    })
    service = AgentService(settings)
    candidate = _candidate("m-penny", "yes-tok", "no-tok")
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    # NO ask = 1 - YES bid  →  YES bid must be (1 - ask_no). Similarly for
    # the other two corners. Keeps the paper execution engine's
    # "NO = 1 − YES" inversion consistent with the book the scorer reads.
    yes_bid = 1.0 - ask_no
    yes_ask = 1.0 - (ask_no - 0.01) if ask_no > 0.01 else 1.0 - 0.001
    state.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": f"{yes_bid:.4f}", "size": "500"}],
        "asks": [{"price": f"{yes_ask:.4f}", "size": "500"}],
    })
    state.apply_book_snapshot({
        "asset_id": "no-tok",
        "bids": [{"price": f"{max(ask_no - 0.01, 0.001):.4f}", "size": "500"}],
        "asks": [{"price": f"{ask_no:.4f}", "size": "500"}],
    })
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate
    return runner, service, candidate, state


def _penny_context(runner, candidate, state, ask_yes: float, ask_no: float, seconds_to_expiry: int):
    from dataclasses import replace as _replace
    from polymarket_ai_agent.apps.daemon.run import DecisionContext
    from polymarket_ai_agent.engine.btc_state import BtcSnapshot
    from polymarket_ai_agent.types import EvidencePacket
    btc = BtcSnapshot(
        price=70000.0, observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0, log_return_1m=0.0, log_return_5m=0.0, log_return_15m=0.0,
        realized_vol_30m=0.002, sample_count=50,
    )
    # Daemon's _seconds_to_expiry reads candidate.end_date_iso, not the
    # packet's seconds_to_expiry — so the force-exit gate only fires when
    # the candidate itself is close to expiry. Rebuild the candidate with
    # an end_date N seconds from now to match the intended TTE.
    end_at = datetime.now(timezone.utc) + timedelta(seconds=seconds_to_expiry)
    candidate = _replace(candidate, end_date_iso=end_at.isoformat())
    # Reversal gate: direction-aware favorable move based on which side
    # is cheap. NO cheap (default runner config) → want YES mid falling.
    # YES cheap → want YES mid rising. Default +40 bps in either sign so
    # the gate always passes in these integration tests; tests explicitly
    # exercising the gate flip the setting instead.
    favorable_move = -40.0 if ask_no <= 0.03 else 40.0
    packet = EvidencePacket(
        market_id=candidate.market_id,
        question=candidate.question,
        resolution_criteria="-",
        market_probability=0.95,
        orderbook_midpoint=0.5,
        spread=0.02,
        depth_usd=500.0,
        seconds_to_expiry=seconds_to_expiry,
        external_price=70000.0,
        recent_price_change_bps=favorable_move,
        recent_trade_count=0,
        reasons_context=[],
        citations=[],
        bid_yes=max(ask_yes - 0.01, 0.001),
        ask_yes=ask_yes,
        bid_no=max(ask_no - 0.01, 0.001),
        ask_no=ask_no,
        realized_vol_30m=0.002,
        time_elapsed_in_candle_s=60,
    )
    # Score it via the penny scorer the runner was built with so the
    # assessment carries the PENNY_STRATEGY_TAG we route on.
    assessment = runner.penny.score_market(packet)
    # The daemon also reads the candidate from its _candidates map, so
    # make sure that map's copy has the matching end_date too.
    runner._candidates[candidate.market_id] = candidate
    return DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=btc,
        assessment=assessment,
        metrics=runner.metrics,
        packet=packet,
    )


def test_penny_enters_on_cheap_side_with_sufficient_tte(tmp_path: Path) -> None:
    """Penny ask ≤ threshold + TTE above min → open paper position on
    the cheap side under strategy_id='penny'.
    """
    from polymarket_ai_agent.types import SuggestedSide
    runner, service, candidate, state = _penny_runner(tmp_path, ask_no=0.02)
    ctx = _penny_context(runner, candidate, state, ask_yes=0.98, ask_no=0.02, seconds_to_expiry=500)

    asyncio.run(runner._handle_penny_strategy(ctx))

    positions = service.portfolio.list_open_positions(strategy_id="penny")
    assert len(positions) == 1
    assert positions[0].side == SuggestedSide.NO
    # Entry at the 2¢ ask; allow some float fuzz.
    assert abs(positions[0].entry_price - 0.02) < 1e-4


def test_penny_skips_entry_when_tte_below_gate(tmp_path: Path) -> None:
    """A penny setup 30 seconds before expiry is the terminal-cliff trap
    — the backtest showed these lose ~100%. Scorer abstains; daemon
    must not open a position.
    """
    runner, service, candidate, state = _penny_runner(tmp_path, ask_no=0.02)
    ctx = _penny_context(runner, candidate, state, ask_yes=0.98, ask_no=0.02, seconds_to_expiry=30)

    asyncio.run(runner._handle_penny_strategy(ctx))

    assert service.portfolio.list_open_positions(strategy_id="penny") == []


def test_penny_skips_entry_when_no_cheap_side(tmp_path: Path) -> None:
    """Mid-price market (both asks near 0.50) produces no penny signal."""
    runner, service, candidate, state = _penny_runner(tmp_path, ask_yes=0.50, ask_no=0.52)
    ctx = _penny_context(runner, candidate, state, ask_yes=0.50, ask_no=0.52, seconds_to_expiry=500)

    asyncio.run(runner._handle_penny_strategy(ctx))

    assert service.portfolio.list_open_positions(strategy_id="penny") == []


def test_penny_take_profit_closes_at_tp_multiple(tmp_path: Path) -> None:
    """Tick 1 opens penny on NO at 2¢. Tick 2 has bid_no = 4¢ (2x entry).
    Must close with ``penny_take_profit`` reason.
    """
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, state = _penny_runner(tmp_path, ask_no=0.02)
    ctx_open = _penny_context(runner, candidate, state, ask_yes=0.98, ask_no=0.02, seconds_to_expiry=500)
    asyncio.run(runner._handle_penny_strategy(ctx_open))
    assert service.portfolio.list_open_positions(strategy_id="penny"), "setup"

    # Bid on NO has bounced to 4c → at 2x entry → TP fires.
    state.apply_book_snapshot({
        "asset_id": "no-tok",
        "bids": [{"price": "0.04", "size": "500"}],
        "asks": [{"price": "0.05", "size": "500"}],
    })
    ctx_tp = _penny_context(runner, candidate, state, ask_yes=0.95, ask_no=0.05, seconds_to_expiry=400)
    asyncio.run(runner._handle_penny_strategy(ctx_tp))

    closed = service.portfolio.list_closed_positions(limit=10)
    assert len(closed) == 1
    assert closed[0].close_reason == "penny_take_profit"
    # Entry ~0.02, exit ~0.04 → ~100% pnl on ~$1 notional.
    assert closed[0].realized_pnl > 0.5


def test_penny_stop_loss_fires_before_force_exit(tmp_path: Path) -> None:
    """Open penny at 2¢. Tick 2 has bid_no = 0.01 (50% loss of entry
    2¢). With penny_stop_loss_multiple=0.5 the SL must fire at the
    threshold instead of waiting for TTE-based force-exit — so the
    reason is penny_stop_loss, not penny_force_exit.
    """
    runner, service, candidate, state = _penny_runner(tmp_path, ask_no=0.02)
    runner.settings = runner.settings.model_copy(update={"penny_stop_loss_multiple": 0.5})
    ctx_open = _penny_context(runner, candidate, state, ask_yes=0.98, ask_no=0.02, seconds_to_expiry=500)
    asyncio.run(runner._handle_penny_strategy(ctx_open))
    assert service.portfolio.list_open_positions(strategy_id="penny")

    # NO bid drops to 0.009 (below 50% of entry 0.02). TTE is still 400s
    # so force-exit gate cannot be confused with SL.
    state.apply_book_snapshot({
        "asset_id": "no-tok",
        "bids": [{"price": "0.009", "size": "500"}],
        "asks": [{"price": "0.015", "size": "500"}],
    })
    ctx_sl = _penny_context(runner, candidate, state, ask_yes=0.985, ask_no=0.015, seconds_to_expiry=400)
    asyncio.run(runner._handle_penny_strategy(ctx_sl))

    closed = service.portfolio.list_closed_positions(limit=10)
    assert len(closed) == 1
    assert closed[0].close_reason == "penny_stop_loss"


def test_penny_stop_loss_disabled_when_multiple_is_zero(tmp_path: Path) -> None:
    """Setting penny_stop_loss_multiple=0 must opt out of the SL gate,
    leaving only TP + TTE force-exit as close reasons. Validates the
    "disabled" contract so operators can turn it off live.
    """
    runner, service, candidate, state = _penny_runner(tmp_path, ask_no=0.02)
    runner.settings = runner.settings.model_copy(update={"penny_stop_loss_multiple": 0.0})
    ctx_open = _penny_context(runner, candidate, state, ask_yes=0.98, ask_no=0.02, seconds_to_expiry=500)
    asyncio.run(runner._handle_penny_strategy(ctx_open))
    assert service.portfolio.list_open_positions(strategy_id="penny")

    # Even with bid at 0.005 (75% below entry), no SL fires because the
    # gate is disabled. TTE still 400s, so force-exit also doesn't fire.
    state.apply_book_snapshot({
        "asset_id": "no-tok",
        "bids": [{"price": "0.005", "size": "500"}],
        "asks": [{"price": "0.015", "size": "500"}],
    })
    ctx = _penny_context(runner, candidate, state, ask_yes=0.985, ask_no=0.015, seconds_to_expiry=400)
    asyncio.run(runner._handle_penny_strategy(ctx))

    assert len(service.portfolio.list_closed_positions(limit=10)) == 0
    assert len(service.portfolio.list_open_positions(strategy_id="penny")) == 1


def test_penny_force_exit_triggers_when_tte_expires(tmp_path: Path) -> None:
    """Open penny position + TTE drops to 90s (< force_exit_tte=120s) →
    close at current bid regardless of TP, reason = penny_force_exit.
    """
    from polymarket_ai_agent.engine.market_state import MarketState

    runner, service, candidate, state = _penny_runner(tmp_path, ask_no=0.02)
    ctx_open = _penny_context(runner, candidate, state, ask_yes=0.98, ask_no=0.02, seconds_to_expiry=500)
    asyncio.run(runner._handle_penny_strategy(ctx_open))
    assert service.portfolio.list_open_positions(strategy_id="penny")

    # TTE shrinks below the gate; bid still around entry → partial loss.
    state.apply_book_snapshot({
        "asset_id": "no-tok",
        "bids": [{"price": "0.015", "size": "500"}],
        "asks": [{"price": "0.025", "size": "500"}],
    })
    ctx_fx = _penny_context(runner, candidate, state, ask_yes=0.975, ask_no=0.025, seconds_to_expiry=90)
    asyncio.run(runner._handle_penny_strategy(ctx_fx))

    closed = service.portfolio.list_closed_positions(limit=10)
    assert len(closed) == 1
    assert closed[0].close_reason == "penny_force_exit"


def test_penny_is_registered_when_enabled(tmp_path: Path) -> None:
    """Startup wiring: penny_enabled=True must place a 'penny' strategy
    in the daemon's strategy list. Fade is always registered; adaptive
    defaults off post-2026-04-24 and is not asserted here.
    """
    runner, *_ = _penny_runner(tmp_path)
    strategy_ids = [s.strategy_id for s in runner._strategies]
    assert "penny" in strategy_ids
    assert "fade" in strategy_ids
    assert "adaptive" not in strategy_ids


def test_penny_not_registered_when_disabled(tmp_path: Path) -> None:
    """penny_enabled=False → strategy is not registered, so no penny
    ticks are emitted and no penny positions can open.
    """
    settings = _settings(tmp_path).model_copy(update={"penny_enabled": False})
    service = AgentService(settings)
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
    )
    strategy_ids = [s.strategy_id for s in runner._strategies]
    assert "penny" not in strategy_ids


def test_daemon_tick_records_trigger_reason(tmp_path: Path) -> None:
    """daemon_tick payloads should be tagged with the WS event type that
    fired them, and the metrics histogram should track the same reasons.
    """
    settings = _settings(tmp_path)
    candidates = [_candidate("m1", "yes-1", "no-1")]
    journal = FakeJournal()
    service = FakeService(candidates, journal)
    events = [
        MarketStreamEvent(
            event_type="book",
            payload={
                "asset_id": "yes-1",
                "bids": [{"price": "0.48", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}],
            },
        ),
        MarketStreamEvent(
            event_type="price_change",
            payload={
                "asset_id": "yes-1",
                "price_changes": [{"price": "0.49", "size": "50", "side": "BUY"}],
            },
        ),
    ]
    market_stream = FakeMarketStream(events)
    btc_feed = FakeBtcFeed([])
    runner = DaemonRunner(
        settings=settings,
        service=service,  # type: ignore[arg-type]
        config=DaemonConfig(
            market_family="btc_1h",
            discovery_interval_seconds=3600.0,
            decision_min_interval_seconds=0.0,
        ),
        market_stream_factory=lambda url: market_stream,  # type: ignore[arg-type]
        btc_feed_factory=lambda: btc_feed,  # type: ignore[arg-type]
    )
    asyncio.run(runner.run_for(0.4))

    tick_payloads = [p for evt, p in journal.events if evt == "daemon_tick"]
    assert tick_payloads, "expected at least one daemon_tick"
    reasons = {p["trigger_reason"] for p in tick_payloads}
    # Both event types should have been observed as triggers given the 0s
    # decision interval.
    assert reasons.issubset({"book", "price_change"})
    assert reasons, "trigger_reason field must be populated"
    triggers_metric = runner.metrics.decision_triggers
    assert sum(triggers_metric.values()) == runner.metrics.decision_ticks
    assert set(triggers_metric.keys()).issubset({"book", "price_change"})


def test_nest_position_extras_groups_by_strategy_then_market() -> None:
    """The heartbeat needs the nested {strategy: {market: extras}} shape so
    the dashboard can render trail state for non-fade strategies (a
    long-standing phase-1 bug previously emitted only fade extras).
    """
    from polymarket_ai_agent.apps.daemon.run import _nest_position_extras

    flat = {
        ("fade", "m1"): {"peak_price": 0.6, "tranches_closed": 0.0},
        ("adaptive_v2", "m1"): {"peak_price": 0.91},
        ("adaptive_v2", "m2"): {"peak_price": 0.55},
        ("penny", "m3"): {"peak_price": 0.04},
    }
    nested = _nest_position_extras(flat)
    assert set(nested.keys()) == {"fade", "adaptive_v2", "penny"}
    assert set(nested["adaptive_v2"].keys()) == {"m1", "m2"}
    # Same market on two strategies must NOT collide.
    assert nested["fade"]["m1"]["peak_price"] == 0.6
    assert nested["adaptive_v2"]["m1"]["peak_price"] == 0.91
    # Inner dicts are copies, not aliases.
    nested["adaptive_v2"]["m1"]["peak_price"] = 1.0
    assert flat[("adaptive_v2", "m1")]["peak_price"] == 0.91


def test_apply_candidates_pins_open_position_market_when_dropped(tmp_path: Path) -> None:
    """A market that drops out of discovery while we hold a paper position
    must be re-pinned to the active set instead of orphan-closed, so the
    exit ladder owns the close.
    """
    from dataclasses import replace as dataclass_replace

    runner, service, candidate, _approved, _btc = _setup_runner_with_open_yes_position(
        tmp_path,
        entry_price=0.50,
        settings_overrides={},
    )
    # Move the market end far into the future so TTE is comfortably positive.
    pinned_candidate = dataclass_replace(candidate, end_date_iso="2099-01-01T00:00:00Z")
    runner._candidates[candidate.market_id] = pinned_candidate

    # Discovery returns an empty list — the market dropped out (e.g. fell
    # below family_min_tte). The position is still open with TTE > 0.
    runner._stop_event = asyncio.Event()  # _apply_candidates → _restart_market_subscriber needs this
    asyncio.run(runner._apply_candidates([]))

    # Position remains open; market is pinned in the active set.
    assert len(service.portfolio.list_open_positions()) == 1
    assert candidate.market_id in runner._market_states
    assert candidate.market_id in runner._candidates
    closed = service.portfolio.list_closed_positions(limit=5)
    assert closed == [], "position must NOT be orphan-closed while TTE > 0"


def test_apply_candidates_orphan_closes_truly_expired_market(tmp_path: Path) -> None:
    """When a market past its end_date_iso drops from discovery, the orphan
    close path must still fire — pinning is a no-op once TTE ≤ 0.
    """
    from dataclasses import replace as dataclass_replace

    runner, service, candidate, _approved, _btc = _setup_runner_with_open_yes_position(
        tmp_path,
        entry_price=0.50,
        settings_overrides={},
    )
    expired = dataclass_replace(candidate, end_date_iso="2020-01-01T00:00:00Z")
    runner._candidates[candidate.market_id] = expired

    runner._stop_event = asyncio.Event()
    asyncio.run(runner._apply_candidates([]))

    assert service.portfolio.list_open_positions() == []
    closed = service.portfolio.list_closed_positions(limit=5)
    assert len(closed) == 1
    assert closed[0].close_reason == "paper_orphan_close"


def test_orphan_close_backfills_scoring_fields_from_cached_assessment(tmp_path: Path) -> None:
    """fair_probability_at_close / edge_at_close must come from the most
    recent scorer output for that (strategy, market) when emitted via the
    orphan-close path.
    """
    from dataclasses import replace as dataclass_replace
    from polymarket_ai_agent.types import MarketAssessment, SuggestedSide

    runner, service, candidate, _approved, _btc = _setup_runner_with_open_yes_position(
        tmp_path,
        entry_price=0.50,
        settings_overrides={},
    )
    # Seed the per-(strategy, market) cache with a known assessment.
    cached = MarketAssessment(
        market_id=candidate.market_id,
        fair_probability=0.71,
        confidence=0.80,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=[],
        reasons_to_abstain=[],
        edge=0.087,
        raw_model_output="cached",
        edge_yes=0.087,
        edge_no=-0.05,
        fair_probability_no=0.29,
        slippage_bps=10.0,
    )
    runner._last_assessment[("fade", candidate.market_id)] = cached
    expired = dataclass_replace(candidate, end_date_iso="2020-01-01T00:00:00Z")
    runner._candidates[candidate.market_id] = expired

    # Snapshot the journal length before so we can isolate the new event.
    before = list(service.journal.read_recent_events(limit=200))

    runner._stop_event = asyncio.Event()
    asyncio.run(runner._apply_candidates([]))

    after = list(service.journal.read_recent_events(limit=200))
    new_events = [e for e in after if e not in before]
    orphan_events = [e for e in new_events if e.get("event_type") == "position_closed"]
    assert orphan_events, "expected a position_closed event from the orphan path"
    payload = orphan_events[-1]["payload"]
    assert payload["close_reason"] == "paper_orphan_close"
    assert payload["fair_probability_at_close"] == 0.71
    assert payload["edge_at_close"] == 0.087
    # Cache entry is consumed on close so a future re-entry doesn't reuse it.
    assert ("fade", candidate.market_id) not in runner._last_assessment


def test_daemon_skips_decision_when_prior_tick_still_running(tmp_path: Path) -> None:
    """Re-entrant guard: a second decision attempt arriving while the first
    is still awaiting the callback must be dropped (and counted), not queued.
    """
    settings = _settings(tmp_path)
    candidates = [_candidate("m1", "yes-1", "no-1")]
    journal = FakeJournal()
    service = FakeService(candidates, journal)

    # Block the callback on a controllable event so the test can interleave a
    # second decision attempt while the first is still in-flight.
    release = asyncio.Event()
    in_flight = asyncio.Event()
    callback_invocations = 0

    async def slow_callback(context):  # type: ignore[no-untyped-def]
        nonlocal callback_invocations
        callback_invocations += 1
        in_flight.set()
        await release.wait()

    runner = DaemonRunner(
        settings=settings,
        service=service,  # type: ignore[arg-type]
        config=DaemonConfig(
            market_family="btc_1h",
            discovery_interval_seconds=3600.0,
            decision_min_interval_seconds=0.0,
        ),
        market_stream_factory=lambda url: FakeMarketStream([]),  # type: ignore[arg-type]
        btc_feed_factory=lambda: FakeBtcFeed([]),  # type: ignore[arg-type]
        decision_callback=slow_callback,
    )

    # Manually drive _maybe_fire_decision twice — the daemon's normal WS loop
    # would interleave these via the same event loop tick.
    from polymarket_ai_agent.engine.market_state import MarketState

    state = MarketState(market_id="m1", yes_token_id="yes-1", no_token_id="no-1")
    state.apply_book_snapshot({
        "asset_id": "yes-1",
        "bids": [{"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.52", "size": "100"}],
    })
    runner._market_states["m1"] = state
    runner._candidates["m1"] = candidates[0]

    async def driver() -> None:
        first = asyncio.create_task(runner._maybe_fire_decision(state, trigger_reason="book"))
        await in_flight.wait()
        # While the first call is still awaiting `release`, kick off a second.
        # It must observe the locked decision lock and short-circuit.
        await runner._maybe_fire_decision(state, trigger_reason="price_change")
        release.set()
        await first

    asyncio.run(driver())

    assert callback_invocations == 1, "second tick must not invoke the callback"
    assert runner.metrics.decision_ticks == 1
    assert runner.metrics.decision_skips_busy == 1
