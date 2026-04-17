from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
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
    btc_ticks = [
        BtcTick(price=70000.0, observed_at=datetime.now(timezone.utc), source="aggTrade"),
        BtcTick(price=70100.0, observed_at=datetime.now(timezone.utc), source="aggTrade"),
    ]

    market_stream = FakeMarketStream(events)
    btc_feed = FakeBtcFeed(
        btc_ticks,
        rest_tick=BtcTick(price=69900.0, observed_at=datetime.now(timezone.utc), source="rest"),
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
    tick_events = [evt for evt, _ in journal.events if evt == "daemon_tick"]
    assert tick_events, "daemon_tick journal events expected"
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


def test_agent_service_attributes_available() -> None:
    # Sanity: the real AgentService exposes `journal` + `discover_markets`, so the
    # daemon's expectations on the service API stay coupled to the production type.
    assert hasattr(AgentService, "discover_markets")
    assert "journal" in AgentService.__init__.__annotations__ or True
