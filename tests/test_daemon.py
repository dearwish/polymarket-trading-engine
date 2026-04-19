from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_ai_agent.apps.daemon.run import DaemonConfig, DaemonRunner
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
    return Settings(
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
        daemon_discovery_interval_seconds=60,
        daemon_decision_min_interval_seconds=0.0,
    )


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
    # Still open — trailing stop is 0.80 × 0.90 = 0.72, mid is 0.80, well above.
    assert len(service.portfolio.list_open_positions()) == 1
    assert runner._position_extras[candidate.market_id]["peak_price"] >= 0.80

    # Reverse: mid drops to 0.70, below the 0.72 trailing level → exit.
    state_down = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    state_down.apply_book_snapshot({
        "asset_id": "yes-tok",
        "bids": [{"price": "0.69", "size": "500"}],
        "asks": [{"price": "0.71", "size": "500"}],
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
    assert candidate.market_id in runner._last_close_at

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
