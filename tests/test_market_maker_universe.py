"""Daemon integration tests for the MM universe wiring.

Locks in:

- the ``universe_filter`` predicate is honoured during dispatch (MM
  strategy fires only on MM markets, BTC strategies only on non-MM
  markets);
- ``_apply_candidates`` merges scanner results into ``_market_states``;
- the scanner is throttled by ``mm_universe_refresh_seconds``;
- ``mm_universe_enabled=False`` reverts to legacy "every strategy on
  every market" behaviour.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_trading_engine.apps.daemon.run import (
    DaemonConfig,
    DaemonRunner,
    DecisionContext,
    StrategyConfig,
)
from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.engine.btc_state import BtcSnapshot
from polymarket_trading_engine.engine.market_state import MarketState
from polymarket_trading_engine.engine.migrations import MigrationRunner
from polymarket_trading_engine.service import AgentService
from polymarket_trading_engine.types import (
    MarketAssessment,
    MarketCandidate,
    SuggestedSide,
)


def _candidate(market_id: str, *, slug: str = "", rewards: float = 0.0) -> MarketCandidate:
    end_iso = (
        (datetime.now(timezone.utc) + timedelta(seconds=3600))
        .isoformat()
        .replace("+00:00", "Z")
    )
    return MarketCandidate(
        market_id=market_id,
        question=f"Q {market_id}",
        condition_id=f"cond-{market_id}",
        slug=slug or f"slug-{market_id}",
        end_date_iso=end_iso,
        yes_token_id=f"{market_id}-yes",
        no_token_id=f"{market_id}-no",
        implied_probability=0.5,
        liquidity_usd=10_000.0,
        volume_24h_usd=20_000.0,
        rewards_daily_rate=rewards,
        rewards_max_spread_pct=3.0 if rewards > 0 else 0.0,
        rewards_min_size=100.0 if rewards > 0 else 0.0,
    )


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        openrouter_api_key="",
        market_family="btc_15m",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
        heartbeat_path=tmp_path / "data" / "daemon_heartbeat.json",
        daemon_decision_min_interval_seconds=0.0,
        min_candle_elapsed_seconds=0,
        position_force_exit_tte_seconds=0,
        min_entry_tte_seconds=0,
        max_consecutive_losses=0,
        mm_enabled=True,
        mm_universe_enabled=True,
        mm_universe_min_rewards_daily_usd=1.0,
        mm_universe_min_liquidity_usd=1_000.0,
        mm_universe_min_tte_seconds=300,
        mm_universe_max_markets=5,
        mm_universe_refresh_seconds=300,
        # Disable other strategies so the strategy list under test is small.
        adaptive_v2_enabled=False,
        penny_enabled=False,
        adaptive_enabled=False,
    )
    base.update(overrides)
    s = Settings(**base)
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    MigrationRunner(s.db_path).run()
    return s


class _DummyStream:
    async def run(self, asset_ids, stop_event=None):
        if stop_event is not None:
            await stop_event.wait()
        if False:  # pragma: no cover
            yield None  # type: ignore[unreachable]


class _DummyFeed:
    def rest_price(self):
        return None

    def rest_klines(self, *a, **kw):
        return []

    async def run(self, stop_event=None):
        if stop_event is not None:
            await stop_event.wait()
        if False:  # pragma: no cover
            yield None  # type: ignore[unreachable]


def _build_runner(tmp_path: Path, **overrides) -> tuple[DaemonRunner, AgentService]:
    settings = _settings(tmp_path, **overrides)
    service = AgentService(settings)
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: _DummyStream(),  # type: ignore[arg-type]
        btc_feed_factory=lambda: _DummyFeed(),  # type: ignore[arg-type]
    )
    return runner, service


def test_strategies_have_universe_filters_when_mm_universe_on(tmp_path: Path) -> None:
    """The fade scorer must carry a btc_filter (skips MM markets); the
    MM scorer must carry an mm_filter (skips non-MM markets).
    """
    runner, _ = _build_runner(tmp_path, mm_enabled=True, mm_universe_enabled=True)

    by_id = {s.strategy_id: s for s in runner._strategies}
    assert "fade" in by_id and "market_maker" in by_id
    assert by_id["fade"].universe_filter is not None
    assert by_id["market_maker"].universe_filter is not None


def test_strategies_have_no_filters_when_mm_universe_off(tmp_path: Path) -> None:
    """Legacy mode: every strategy runs on every market. Both filters None."""
    runner, _ = _build_runner(tmp_path, mm_enabled=True, mm_universe_enabled=False)

    by_id = {s.strategy_id: s for s in runner._strategies}
    assert by_id["fade"].universe_filter is None
    assert by_id["market_maker"].universe_filter is None


def test_universe_filter_partitions_dispatch(tmp_path: Path) -> None:
    """An MM market triggers the MM strategy only; a BTC market triggers
    the fade strategy only. Verified by recording which strategy_ids
    ``_run_strategy_tick`` is called with — that's the post-filter
    dispatch point.
    """
    runner, service = _build_runner(tmp_path)
    runner._mm_market_ids = {"mm-1"}
    runner._strategies = runner._build_strategies(runner.settings)

    invoked: list[tuple[str, str]] = []

    async def _record(context, strategy):
        invoked.append((strategy.strategy_id, context.market_id))

    runner._run_strategy_tick = _record  # type: ignore[assignment]

    btc_candidate = _candidate("btc-1")
    mm_candidate = _candidate("mm-1", rewards=10.0)
    state_btc = MarketState(
        market_id=btc_candidate.market_id,
        yes_token_id=btc_candidate.yes_token_id,
        no_token_id=btc_candidate.no_token_id,
    )
    state_btc.apply_book_snapshot({
        "asset_id": btc_candidate.yes_token_id,
        "bids": [{"price": "0.50", "size": "500"}],
        "asks": [{"price": "0.54", "size": "500"}],
    })
    state_mm = MarketState(
        market_id=mm_candidate.market_id,
        yes_token_id=mm_candidate.yes_token_id,
        no_token_id=mm_candidate.no_token_id,
    )
    state_mm.apply_book_snapshot({
        "asset_id": mm_candidate.yes_token_id,
        "bids": [{"price": "0.50", "size": "500"}],
        "asks": [{"price": "0.54", "size": "500"}],
    })

    btc_snap = BtcSnapshot(
        price=70_000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0,
        log_return_1m=0.0,
        log_return_5m=0.0,
        log_return_15m=0.0,
        realized_vol_30m=0.01,
        sample_count=50,
    )
    placeholder_assessment = MarketAssessment(
        market_id="x",
        fair_probability=0.5,
        confidence=0.0,
        suggested_side=SuggestedSide.ABSTAIN,
        expiry_risk="LOW",
        reasons_for_trade=[],
        reasons_to_abstain=[],
        edge=0.0,
        raw_model_output="stub",
    )

    btc_ctx = DecisionContext(
        market_id=btc_candidate.market_id,
        candidate=btc_candidate,
        features=state_btc.features(),
        btc_snapshot=btc_snap,
        assessment=placeholder_assessment,
        metrics=runner.metrics,
    )
    mm_ctx = DecisionContext(
        market_id=mm_candidate.market_id,
        candidate=mm_candidate,
        features=state_mm.features(),
        btc_snapshot=btc_snap,
        assessment=placeholder_assessment,
        metrics=runner.metrics,
    )

    asyncio.run(runner._paper_execute_decision_callback(btc_ctx))
    asyncio.run(runner._paper_execute_decision_callback(mm_ctx))

    # fade only fired on the BTC market; market_maker only on the MM market.
    assert ("fade", "btc-1") in invoked
    assert ("market_maker", "mm-1") in invoked
    assert ("fade", "mm-1") not in invoked
    assert ("market_maker", "btc-1") not in invoked


def test_refresh_mm_universe_calls_scanner_once_and_caches(tmp_path: Path) -> None:
    """Two ``_refresh_mm_universe`` calls inside the refresh window must
    issue exactly one scan to the connector.
    """
    runner, service = _build_runner(tmp_path, mm_universe_refresh_seconds=300)
    scan_calls: list[dict] = []
    fake_candidates = [_candidate("mm-a", rewards=15.0), _candidate("mm-b", rewards=8.0)]

    def fake_discover(**kwargs):
        scan_calls.append(kwargs)
        return fake_candidates

    service.polymarket.discover_mm_markets = fake_discover  # type: ignore[assignment]

    first = asyncio.run(runner._refresh_mm_universe())
    second = asyncio.run(runner._refresh_mm_universe())

    assert first == {"mm-a", "mm-b"}
    assert second == {"mm-a", "mm-b"}
    assert len(scan_calls) == 1


def test_refresh_mm_universe_returns_empty_when_disabled(tmp_path: Path) -> None:
    runner, service = _build_runner(tmp_path, mm_universe_enabled=False)
    called = False

    def fake_discover(**kwargs):
        nonlocal called
        called = True
        return [_candidate("mm-x", rewards=10.0)]

    service.polymarket.discover_mm_markets = fake_discover  # type: ignore[assignment]
    result = asyncio.run(runner._refresh_mm_universe())
    assert result == set()
    assert not called


def test_apply_candidates_merges_mm_markets_into_active_set(tmp_path: Path) -> None:
    """The discovery loop integration: BTC universe + MM universe coexist
    in ``_market_states`` and ``_active_asset_ids`` after one cycle.
    """
    runner, service = _build_runner(tmp_path)
    fake_mm = _candidate("mm-1", rewards=12.0)

    def fake_discover(**kwargs):
        return [fake_mm]

    service.polymarket.discover_mm_markets = fake_discover  # type: ignore[assignment]
    btc_candidate = _candidate("btc-1", slug="btc-updown-15m-x")

    async def _run() -> None:
        # ``_apply_candidates`` will call ``_restart_market_subscriber`` if
        # asset_ids changes — that requires a live event loop AND a
        # ``_stop_event``. Provide both, then await alongside.
        runner._stop_event = asyncio.Event()
        await runner._apply_candidates([btc_candidate])
        # Cancel the subscriber task so the test exits cleanly.
        if runner._market_subscriber_task is not None:
            runner._market_subscriber_task.cancel()
            try:
                await runner._market_subscriber_task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())

    assert btc_candidate.market_id in runner._market_states
    assert fake_mm.market_id in runner._market_states
    assert runner._mm_market_ids == {fake_mm.market_id}
    assert btc_candidate.yes_token_id in runner._active_asset_ids
    assert fake_mm.yes_token_id in runner._active_asset_ids
