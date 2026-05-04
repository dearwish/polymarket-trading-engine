"""Integration tests for the market-maker daemon lifecycle handler.

Exercises ``DaemonRunner._handle_market_maker_strategy`` against a real
``AgentService`` (SQLite + journal) but stubbed WS factories. Locks in:

- two-sided quote placement on the first tick,
- per-leg fill recording when the ask crosses our limit,
- inventory-skew shifts after a fill,
- one-sided halt when the inventory cap fires,
- force-exit at the TTE buffer closing every leg cleanly.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import replace as dataclass_replace

from polymarket_trading_engine.apps.daemon.run import (
    DaemonConfig,
    DaemonRunner,
    DecisionContext,
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


def _candidate(market_id: str, yes: str = "yes-tok", no: str = "no-tok") -> MarketCandidate:
    # Short TTE so the force-exit test can reach it; far enough out that
    # the default 120s min-TTE gate doesn't abstain. We'll override
    # end_date_iso explicitly per test where needed.
    end_iso = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    return MarketCandidate(
        market_id=market_id,
        question=f"MM market {market_id}",
        condition_id=f"cond-{market_id}",
        slug=f"slug-{market_id}",
        end_date_iso=end_iso,
        yes_token_id=yes,
        no_token_id=no,
        implied_probability=0.5,
        liquidity_usd=10_000.0,
        volume_24h_usd=20_000.0,
    )


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
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
        min_candle_elapsed_seconds=0,
        position_force_exit_tte_seconds=0,
        min_entry_tte_seconds=0,
        max_consecutive_losses=0,
        # MM-specific overrides — turn on by default for these tests.
        mm_enabled=True,
        mm_size_usd=1.0,
        mm_target_half_spread=0.02,
        mm_min_market_spread=0.01,
        mm_max_market_spread=0.20,
        mm_min_tte_seconds=60,
        mm_inventory_skew_strength=0.5,
        mm_max_inventory_usd=5.0,
        mm_require_rewards=False,
        mm_quote_ttl_seconds=60,
        mm_replace_min_ticks=1.0,
        mm_replace_min_size_pct=0.10,
        mm_force_exit_tte_seconds=30,
    )
    base.update(overrides)
    s = Settings(**base)
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    MigrationRunner(s.db_path).run()
    return s


def _approved_assessment(market_id: str) -> MarketAssessment:
    """The MM scorer returns an APPROVED YES tag when gates pass; we
    hand-build an equivalent assessment so the handler test doesn't
    re-invoke the scorer (which is covered separately).
    """
    return MarketAssessment(
        market_id=market_id,
        fair_probability=0.55,
        confidence=0.5,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=["MM: quoting"],
        reasons_to_abstain=[],
        edge=0.0,
        raw_model_output="market-maker-strategy",
        edge_yes=0.0,
        edge_no=0.0,
        fair_probability_no=0.45,
        slippage_bps=0.0,
    )


def _btc_snapshot() -> BtcSnapshot:
    return BtcSnapshot(
        price=70_000.0,
        observed_at=datetime.now(timezone.utc),
        log_return_10s=0.0,
        log_return_1m=0.0,
        log_return_5m=0.0,
        log_return_15m=0.0,
        realized_vol_30m=0.01,
        sample_count=50,
    )


def _build_runner(tmp_path: Path, **settings_overrides) -> tuple[DaemonRunner, AgentService, MarketCandidate]:
    settings = _settings(tmp_path, **settings_overrides)
    service = AgentService(settings)
    candidate = _candidate("m-mm")
    runner = DaemonRunner(
        settings=settings,
        service=service,
        config=DaemonConfig(market_family=settings.market_family),
        market_stream_factory=lambda url: _DummyStream(),  # type: ignore[arg-type]
        btc_feed_factory=lambda: _DummyFeed(),  # type: ignore[arg-type]
    )
    return runner, service, candidate


class _DummyStream:
    async def run(self, asset_ids, stop_event=None):
        if stop_event is not None:
            await stop_event.wait()
        if False:  # pragma: no cover
            yield None  # type: ignore[unreachable]


class _DummyFeed:
    def rest_price(self):
        return None

    def rest_klines(self, *args, **kwargs):
        return []

    async def run(self, stop_event=None):
        if stop_event is not None:
            await stop_event.wait()
        if False:  # pragma: no cover
            yield None  # type: ignore[unreachable]


def _seed_book(state: MarketState, *, bid: float, ask: float, size: float = 500.0) -> None:
    """Apply a YES book snapshot. NO book stays at parity (1 - prices)."""
    state.apply_book_snapshot({
        "asset_id": state.yes_token_id,
        "bids": [{"price": f"{bid:.4f}", "size": f"{size:.0f}"}],
        "asks": [{"price": f"{ask:.4f}", "size": f"{size:.0f}"}],
    })
    state.apply_book_snapshot({
        "asset_id": state.no_token_id,
        "bids": [{"price": f"{1 - ask:.4f}", "size": f"{size:.0f}"}],
        "asks": [{"price": f"{1 - bid:.4f}", "size": f"{size:.0f}"}],
    })


def _context(runner: DaemonRunner, candidate: MarketCandidate, state: MarketState) -> DecisionContext:
    return DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=_btc_snapshot(),
        assessment=_approved_assessment(candidate.market_id),
        metrics=runner.metrics,
    )


def test_handler_places_two_sided_quotes_on_first_tick(tmp_path: Path) -> None:
    runner, service, candidate = _build_runner(tmp_path)
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))

    # Both legs should be parked. mid=0.52, half_spread=0.02 → yes_bid=0.50, no_bid=0.46.
    yes_key = ("market_maker", candidate.market_id, "YES")
    no_key = ("market_maker", candidate.market_id, "NO")
    assert yes_key in runner._pending_mm_orders
    assert no_key in runner._pending_mm_orders
    yes_order = runner._pending_mm_orders[yes_key]
    no_order = runner._pending_mm_orders[no_key]
    assert yes_order.side == SuggestedSide.YES
    assert no_order.side == SuggestedSide.NO
    assert abs(yes_order.limit_price - 0.50) < 1e-6
    assert abs(no_order.limit_price - 0.46) < 1e-6
    # No filled position yet.
    assert service.portfolio.list_open_positions(strategy_id="market_maker") == []


def test_handler_records_fill_when_ask_crosses_yes_limit(tmp_path: Path) -> None:
    runner, service, candidate = _build_runner(tmp_path)
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Tick 1: place quotes.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    assert yes_key in runner._pending_mm_orders
    yes_limit = runner._pending_mm_orders[yes_key].limit_price  # 0.50

    # Now an aggressive seller drops the YES ask down to our resting bid.
    _seed_book(state, bid=0.48, ask=yes_limit)  # ask now == our limit → fill
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))

    # The YES leg must have been filled and recorded as an open YES position.
    # The handler then re-posts a fresh YES rest at a SKEWED-DOWN price
    # (because we're now long YES) — that's expected behaviour, so we
    # don't assert pending absence here, only that the FILLED leg landed
    # in the portfolio with the original limit price as the entry.
    yes_positions = [
        p for p in service.portfolio.list_open_positions(strategy_id="market_maker")
        if p.side == SuggestedSide.YES
    ]
    assert len(yes_positions) == 1
    assert abs(yes_positions[0].entry_price - yes_limit) < 1e-9
    # The replacement YES quote (if any) must be priced LOWER than the
    # original because the inventory skew is now positive.
    new_yes = runner._pending_mm_orders.get(yes_key)
    if new_yes is not None:
        assert new_yes.limit_price < yes_limit


def test_handler_skews_quotes_after_yes_fill(tmp_path: Path) -> None:
    """After buying YES (long inventory), the YES-buy quote should drop
    and the NO-buy quote should rise. Direction-only check; the magnitude
    is locked in by the quoter unit tests.
    """
    runner, service, candidate = _build_runner(tmp_path, mm_inventory_skew_strength=1.0)
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Tick 1: place quotes.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    no_key = ("market_maker", candidate.market_id, "NO")
    initial_yes = runner._pending_mm_orders[yes_key].limit_price
    initial_no = runner._pending_mm_orders[no_key].limit_price

    # Cross our YES bid → fill it. Reset book to original mid for tick 3 so
    # only inventory drives the new quote (not a mid shift).
    _seed_book(state, bid=0.48, ask=initial_yes)
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    _seed_book(state, bid=0.50, ask=0.54)
    # Force replacement by widening hysteresis-bypassing settings so even
    # a small skew triggers a re-quote.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))

    new_no = runner._pending_mm_orders[no_key].limit_price
    new_yes = runner._pending_mm_orders.get(yes_key)
    # NO leg must have moved UP (we want to flatten via NO buys).
    assert new_no > initial_no
    # YES leg should have moved DOWN (less aggressive on more YES) or be
    # halted if the cap fired.
    if new_yes is not None:
        assert new_yes.limit_price < initial_yes


def test_handler_halts_yes_buys_at_inventory_cap(tmp_path: Path) -> None:
    """When YES exposure hits ``mm_max_inventory_usd``, the YES-buy leg
    must not be posted; the NO-buy leg keeps quoting so a fill flattens
    the inventory. We seed the YES position directly via the portfolio
    rather than going through fill machinery so the test is deterministic
    against book shape.
    """
    runner, service, candidate = _build_runner(
        tmp_path, mm_max_inventory_usd=1.0, mm_size_usd=1.0
    )
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    # Use a wide-spread book so NO ask stays above the NO-buy limit and
    # the NO leg doesn't auto-fill on the same tick.
    _seed_book(state, bid=0.40, ask=0.60)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Seed an existing YES position at-cap directly. Mirrors the row that
    # ``record_execution`` would write after a fill.
    from polymarket_trading_engine.types import (
        DecisionStatus,
        ExecutionMode,
        ExecutionResult,
        ExecutionStyle,
        TradeDecision,
    )
    seed_decision = TradeDecision(
        market_id=candidate.market_id,
        status=DecisionStatus.APPROVED,
        side=SuggestedSide.YES,
        size_usd=1.0,
        limit_price=0.50,
        rationale=["seed"],
        rejected_by=[],
        asset_id="yes-tok",
        execution_style=ExecutionStyle.GTC_MAKER,
        post_only=True,
        strategy_id="market_maker",
    )
    seed_result = ExecutionResult(
        market_id=candidate.market_id,
        success=True,
        mode=ExecutionMode.PAPER,
        order_id="seeded-yes-leg",
        status="FILLED_PAPER",
        detail="seed",
        fill_price=0.50,
        filled_size_shares=2.0,
        remaining_size_shares=0.0,
        execution_style=ExecutionStyle.GTC_MAKER,
    )
    service.portfolio.record_execution(seed_decision, seed_result)

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))

    yes_key = ("market_maker", candidate.market_id, "YES")
    no_key = ("market_maker", candidate.market_id, "NO")
    # YES side halted (cap fired); NO side still quoting.
    assert yes_key not in runner._pending_mm_orders
    assert no_key in runner._pending_mm_orders


def test_handler_force_exits_open_legs_inside_tte_buffer(tmp_path: Path) -> None:
    """When TTE drops below ``mm_force_exit_tte_seconds``, every open MM
    leg must close and pending quotes must be cancelled.
    """
    runner, service, candidate = _build_runner(tmp_path, mm_force_exit_tte_seconds=30)
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Place + fill the YES leg.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    yes_limit = runner._pending_mm_orders[yes_key].limit_price
    _seed_book(state, bid=0.48, ask=yes_limit)
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    assert len(service.portfolio.list_open_positions(strategy_id="market_maker")) == 1

    # Now collapse TTE — set candidate end_date 10s in the future (under the
    # 30s force-exit threshold).
    soon_iso = (
        (datetime.now(timezone.utc) + timedelta(seconds=10))
        .isoformat()
        .replace("+00:00", "Z")
    )
    expired_candidate = dataclass_replace(candidate, end_date_iso=soon_iso)
    runner._candidates[candidate.market_id] = expired_candidate
    ctx = DecisionContext(
        market_id=expired_candidate.market_id,
        candidate=expired_candidate,
        features=state.features(),
        btc_snapshot=_btc_snapshot(),
        assessment=_approved_assessment(expired_candidate.market_id),
        metrics=runner.metrics,
    )
    asyncio.run(runner._handle_market_maker_strategy(ctx))

    # All legs closed; pending quotes cleared.
    assert service.portfolio.list_open_positions(strategy_id="market_maker") == []
    assert not any(
        k for k in runner._pending_mm_orders if k[0] == "market_maker" and k[1] == candidate.market_id
    )
    closed = service.portfolio.list_closed_positions(strategy_id="market_maker", limit=5)
    assert closed
    assert closed[0].close_reason == "mm_force_exit"


def test_handler_skips_when_scorer_abstains_and_cancels_pending(tmp_path: Path) -> None:
    """If the scorer's assessment is ABSTAIN (e.g. spread widened) the
    handler must cancel any resting MM quotes and not place new ones.
    """
    runner, service, candidate = _build_runner(tmp_path)
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    assert ("market_maker", candidate.market_id, "YES") in runner._pending_mm_orders

    # Build an ABSTAIN assessment (e.g. scorer noticed spread widened to toxic).
    abstain = dataclass_replace(
        _approved_assessment(candidate.market_id),
        suggested_side=SuggestedSide.ABSTAIN,
        reasons_to_abstain=["MM: market spread > max"],
    )
    ctx = DecisionContext(
        market_id=candidate.market_id,
        candidate=candidate,
        features=state.features(),
        btc_snapshot=_btc_snapshot(),
        assessment=abstain,
        metrics=runner.metrics,
    )
    asyncio.run(runner._handle_market_maker_strategy(ctx))

    # All MM rests for this market cancelled.
    assert not any(
        k for k in runner._pending_mm_orders if k[1] == candidate.market_id
    )


def test_handler_accrues_reward_for_in_band_quote_over_time(tmp_path: Path) -> None:
    """When a market pays daily rewards and our quote rests in-band, the
    handler must (a) build a ``QuoteAccrualState`` on placement,
    (b) advance it on subsequent ticks, and (c) persist via
    ``record_reward_accrual`` when the quote ends.

    Uses a reward-paying candidate (rewards_daily_rate > 0,
    rewards_max_spread_pct > 0) and seeds a real BTC-snapshot-free
    market so the lifecycle handler runs cleanly.
    """
    from datetime import timedelta as _td

    runner, service, candidate = _build_runner(
        tmp_path, mm_quote_ttl_seconds=60, mm_inventory_skew_strength=0.0
    )
    # Stamp the candidate with reward parameters.
    rewarded = dataclass_replace(
        candidate,
        rewards_daily_rate=864.0,  # 864/day = $0.01/second total pool
        rewards_max_spread_pct=3.0,  # ±3¢ around mid
    )
    runner._candidates[rewarded.market_id] = rewarded

    state = MarketState(
        market_id=rewarded.market_id, yes_token_id="yes-tok", no_token_id="no-tok"
    )
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[rewarded.market_id] = state

    # Tick 1: placement creates accrual state but doesn't credit yet.
    ctx = DecisionContext(
        market_id=rewarded.market_id,
        candidate=rewarded,
        features=state.features(),
        btc_snapshot=_btc_snapshot(),
        assessment=_approved_assessment(rewarded.market_id),
        metrics=runner.metrics,
    )
    asyncio.run(runner._handle_market_maker_strategy(ctx))
    yes_key = ("market_maker", rewarded.market_id, "YES")
    assert yes_key in runner._mm_quote_accrual
    initial_state = runner._mm_quote_accrual[yes_key]
    assert initial_state.cumulative_reward_usd == 0.0

    # Manually advance the accrual state's last_check_at backward by 1
    # hour so the next handler tick credits a full hour. Simulating this
    # directly is cleaner than sleeping — the daemon can't tick with
    # real wall-clock waits in tests.
    runner._mm_quote_accrual[yes_key].last_check_at -= _td(hours=1)
    runner._mm_quote_accrual[
        ("market_maker", rewarded.market_id, "NO")
    ].last_check_at -= _td(hours=1)

    # Tick 2: handler runs accrual loop. Both YES and NO legs are in-band
    # (mid 0.52, half_spread 0.02 → quotes at 0.50 / 0.46, well inside
    # the ±0.03 reward band). Each leg gets ~0.5 × 864/24 = $18 credited.
    asyncio.run(runner._handle_market_maker_strategy(ctx))
    yes_state = runner._mm_quote_accrual[yes_key]
    assert yes_state.in_band_seconds >= 3500.0  # ~1h, allowing for real elapsed
    assert yes_state.cumulative_reward_usd > 0.0
    assert yes_state.pending_reward_usd > 0.0
    # Sanity: a full hour at half the side pool (since both YES + NO
    # legs split it) shouldn't exceed the daily/24 ceiling.
    assert yes_state.cumulative_reward_usd < 864.0 / 24.0

    # Force quote expiry: rewind placed_at far enough that is_expired
    # fires on the next tick. The persist path then writes a
    # reward_accruals row, AND the handler immediately re-quotes a
    # fresh leg with a brand-new (empty) accrual state — so the
    # post-condition is "DB has the previous accrual" not "in-memory
    # state cleared".
    pending_yes = runner._pending_mm_orders[yes_key]
    runner._pending_mm_orders[yes_key] = dataclass_replace(
        pending_yes, placed_at=pending_yes.placed_at - _td(seconds=120)
    )
    pre_total = service.portfolio.total_reward_accrued("market_maker")
    asyncio.run(runner._handle_market_maker_strategy(ctx))
    post_total = service.portfolio.total_reward_accrued("market_maker")
    # The expired quote's accrual was persisted to the DB.
    assert post_total > pre_total
    assert post_total > 0.0
    # The handler re-quoted, so a fresh empty state exists for YES leg.
    fresh = runner._mm_quote_accrual[yes_key]
    assert fresh.cumulative_reward_usd == 0.0
    assert fresh.pending_reward_usd == 0.0


def test_fill_drift_guard_refuses_stale_quote_fill(tmp_path: Path) -> None:
    """Adverse-selection guard: if the mid has crashed since we placed
    our quote (e.g. the market is about to resolve against us), the
    handler must REFUSE to honour the cross instead of catching the
    falling knife. Triggered the 2026-05-02 −$985 losses; this test
    locks in the guard.
    """
    runner, service, candidate = _build_runner(
        tmp_path, mm_max_fill_drift_pct=5.0, mm_size_usd=1000.0
    )
    state = MarketState(
        market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok"
    )
    # Initial book: tight, mid=0.52. Our YES bid will land at ~0.50.
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Tick 1: place quotes at the healthy mid.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    assert yes_key in runner._pending_mm_orders
    placed_yes = runner._pending_mm_orders[yes_key]
    yes_limit = placed_yes.limit_price  # ~0.50

    # SIMULATE THE CRASH: book has crashed to bid=0.01 / ask=0.05 (mid
    # ~0.03 vs our 0.50 quote — 94% drift). Ask 0.05 ≤ our 0.50 limit
    # so the cross trigger fires, BUT the drift guard must refuse the
    # fill because the market has clearly repriced under us.
    _seed_book(state, bid=0.01, ask=0.05, size=500.0)

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))

    # The fill MUST have been refused — no MM YES position recorded.
    # The handler may have re-quoted YES at the new mid (0.01); that's
    # fine and expected. What matters is the OLD 0.50 quote was NOT
    # honoured against the crashed book.
    yes_positions = [
        p for p in service.portfolio.list_open_positions(strategy_id="market_maker")
        if p.side == SuggestedSide.YES
    ]
    assert len(yes_positions) == 0, (
        f"Drift guard failed: a YES position was recorded at "
        f"{yes_positions[0].entry_price if yes_positions else None} "
        f"despite the mid having crashed to ~0.03"
    )
    # Sanity check: if a fresh YES quote was posted, it should reflect
    # the new mid (~0.01), not the stale 0.50.
    fresh_yes = runner._pending_mm_orders.get(yes_key)
    if fresh_yes is not None:
        assert fresh_yes.limit_price < 0.10, (
            f"Re-quoted YES at {fresh_yes.limit_price}, expected near new mid 0.03"
        )


def test_no_fill_tte_guard_refuses_fill_near_resolution(tmp_path: Path) -> None:
    """Open-side mirror of mm_force_exit_tte_seconds. A fill that opens
    a position 30 seconds before resolution can only result in a
    force-close at the resolution price — refuse the open instead.
    """
    from datetime import timedelta as _td

    runner, service, candidate = _build_runner(
        tmp_path,
        mm_no_fill_tte_seconds=60,
        mm_max_fill_drift_pct=0.0,  # disable drift guard so we test TTE in isolation
    )
    state = MarketState(
        market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok"
    )
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    # Place quotes with a healthy TTE.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    yes_limit = runner._pending_mm_orders[yes_key].limit_price

    # Now squeeze the candidate's TTE to 30s — under the 60s no-fill
    # floor — and have the ask cross our limit.
    soon_iso = (
        (datetime.now(timezone.utc) + _td(seconds=30))
        .isoformat()
        .replace("+00:00", "Z")
    )
    expired_candidate = dataclass_replace(candidate, end_date_iso=soon_iso)
    runner._candidates[candidate.market_id] = expired_candidate
    _seed_book(state, bid=0.48, ask=yes_limit)
    ctx = DecisionContext(
        market_id=expired_candidate.market_id,
        candidate=expired_candidate,
        features=state.features(),
        btc_snapshot=_btc_snapshot(),
        assessment=_approved_assessment(expired_candidate.market_id),
        metrics=runner.metrics,
    )
    asyncio.run(runner._handle_market_maker_strategy(ctx))

    # Fill refused; no MM position opened.
    assert (
        len(service.portfolio.list_open_positions(strategy_id="market_maker"))
        == 0
    )


def test_universe_exit_cancels_pending_mm_quotes(tmp_path: Path) -> None:
    """When a market drops from ``_mm_market_ids`` (the scanner stopped
    picking it), all its pending MM quotes must be cancelled. Without
    this, zombie quotes survive in memory until a stray WS event
    reactivates them — which is what caused the 2026-05-02 catastrophic
    fills.
    """
    runner, service, candidate = _build_runner(tmp_path)
    state = MarketState(
        market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok"
    )
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate
    runner._mm_market_ids = {candidate.market_id}

    # Place initial MM quotes.
    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    no_key = ("market_maker", candidate.market_id, "NO")
    assert yes_key in runner._pending_mm_orders
    assert no_key in runner._pending_mm_orders

    # Simulate the next discovery cycle dropping this market from the
    # MM universe. We bypass the full _apply_candidates by stubbing
    # _refresh_mm_universe to return an empty set.
    async def _empty_scan() -> set[str]:
        return set()

    runner._refresh_mm_universe = _empty_scan  # type: ignore[assignment]
    runner._stop_event = asyncio.Event()

    async def _run() -> None:
        await runner._apply_candidates([candidate])  # candidate stays as a BTC market
        if runner._market_subscriber_task is not None:
            runner._market_subscriber_task.cancel()
            try:
                await runner._market_subscriber_task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())

    # Both MM quotes must be gone.
    assert yes_key not in runner._pending_mm_orders
    assert no_key not in runner._pending_mm_orders


def test_max_quote_age_force_cancels_zombie_quote(tmp_path: Path) -> None:
    """Defense-in-depth: a quote older than ``mm_max_quote_age_seconds``
    is force-cancelled regardless of TTL or freshness state. Ensures
    no quote can survive long enough to be adversely selected even if
    every other safety net failed.
    """
    from datetime import timedelta as _td

    runner, service, candidate = _build_runner(
        tmp_path, mm_max_quote_age_seconds=120
    )
    state = MarketState(
        market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok"
    )
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    yes_key = ("market_maker", candidate.market_id, "YES")
    pending = runner._pending_mm_orders[yes_key]
    # Backdate placed_at so the quote appears 5 minutes old (well over
    # the 120s cap). The fill check on the next tick should force-cancel.
    runner._pending_mm_orders[yes_key] = dataclass_replace(
        pending, placed_at=pending.placed_at - _td(seconds=300)
    )

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))

    # The aged quote got force-cancelled. The handler may have placed a
    # fresh one in its place — that's fine; what matters is the OLD
    # quote (with the stale placed_at) is gone.
    new_pending = runner._pending_mm_orders.get(yes_key)
    if new_pending is not None:
        # Re-quote happened; the fresh quote's age must be near zero.
        assert (datetime.now(timezone.utc) - new_pending.placed_at).total_seconds() < 5.0


def test_handler_skips_when_require_rewards_and_market_pays_none(tmp_path: Path) -> None:
    """The reward gate is enforced in the daemon (not the scorer) since
    the candidate carries the rewards_daily_rate field. With it on and
    the candidate paying 0 rewards, no quote should be placed.
    """
    runner, service, candidate = _build_runner(tmp_path, mm_require_rewards=True)
    # Default candidate has rewards_daily_rate=0.0.
    state = MarketState(market_id=candidate.market_id, yes_token_id="yes-tok", no_token_id="no-tok")
    _seed_book(state, bid=0.50, ask=0.54)
    runner._market_states[candidate.market_id] = state
    runner._candidates[candidate.market_id] = candidate

    asyncio.run(runner._handle_market_maker_strategy(_context(runner, candidate, state)))
    assert not runner._pending_mm_orders
