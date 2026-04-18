from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from polymarket_ai_agent.config import (
    FAMILY_PROFILE_OVERRIDES,
    RiskProfile,
    Settings,
    resolve_risk_profile,
)
from polymarket_ai_agent.engine.portfolio import PortfolioEngine
from polymarket_ai_agent.engine.risk import RiskEngine
from polymarket_ai_agent.types import (
    AccountState,
    ExecutionMode,
    MarketAssessment,
    MarketCandidate,
    MarketSnapshot,
    OrderBookSnapshot,
    SuggestedSide,
    utc_now,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        openrouter_api_key="",
        polymarket_private_key="",
        polymarket_funder="",
        polymarket_signature_type=0,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "agent.db",
        events_path=tmp_path / "logs" / "events.jsonl",
        runtime_settings_path=tmp_path / "data" / "runtime_settings.json",
    )
    base.update(overrides)
    return Settings(**base)


def _snapshot(
    spread: float = 0.01,
    depth: float = 500.0,
    seconds_to_expiry: int = 120,
    observed_age_seconds: int = 0,
) -> MarketSnapshot:
    candidate = MarketCandidate(
        market_id="m1",
        question="Will BTC be up in 5 minutes?",
        condition_id="cond-1",
        slug="btc-5m",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id="yes",
        no_token_id="no",
        implied_probability=0.5,
        liquidity_usd=10000.0,
        volume_24h_usd=20000.0,
    )
    return MarketSnapshot(
        candidate=candidate,
        orderbook=OrderBookSnapshot(
            bid=0.51, ask=0.52, midpoint=0.515, spread=spread, depth_usd=depth,
            last_trade_price=0.515,
            observed_at=utc_now() - timedelta(seconds=observed_age_seconds),
        ),
        seconds_to_expiry=seconds_to_expiry,
        recent_price_change_bps=0.0,
        recent_trade_count=0,
        external_price=70000.0,
    )


def _assessment(side: SuggestedSide = SuggestedSide.YES, edge: float = 0.05) -> MarketAssessment:
    return MarketAssessment(
        market_id="m1",
        fair_probability=0.58,
        confidence=0.9,
        suggested_side=side,
        expiry_risk="LOW",
        reasons_for_trade=["edge exists"],
        reasons_to_abstain=[],
        edge=edge,
        raw_model_output="quant-scoring",
    )


def _account_state(
    open_positions: int = 0,
    net_btc_exposure_usd: float = 0.0,
    available_usd: float = 100.0,
) -> AccountState:
    return AccountState(
        mode=ExecutionMode.PAPER,
        available_usd=available_usd,
        open_positions=open_positions,
        daily_realized_pnl=0.0,
        rejected_orders=0,
        long_btc_exposure_usd=max(0.0, net_btc_exposure_usd),
        short_btc_exposure_usd=max(0.0, -net_btc_exposure_usd),
        net_btc_exposure_usd=net_btc_exposure_usd,
        total_exposure_usd=abs(net_btc_exposure_usd),
    )


def test_family_profile_overrides_match_roadmap() -> None:
    # Guardrail that the roadmap's per-family tuning stays in the code.
    assert FAMILY_PROFILE_OVERRIDES["btc_1h"]["stale_data_seconds"] == 5
    assert FAMILY_PROFILE_OVERRIDES["btc_15m"]["stale_data_seconds"] == 3
    assert FAMILY_PROFILE_OVERRIDES["btc_5m"]["stale_data_seconds"] == 2
    assert FAMILY_PROFILE_OVERRIDES["btc_1h"]["max_concurrent_positions"] == 2
    assert FAMILY_PROFILE_OVERRIDES["btc_5m"]["max_concurrent_positions"] == 1


def test_resolve_risk_profile_picks_family_defaults_when_no_global_override(tmp_path: Path) -> None:
    profile = resolve_risk_profile(_settings(tmp_path, market_family="btc_5m"))
    assert profile.family == "btc_5m"
    assert profile.stale_data_seconds == 2
    assert profile.max_concurrent_positions == 1
    assert profile.exit_buffer_pct_of_tte == 0.10


def test_resolve_risk_profile_honors_explicit_global_override(tmp_path: Path) -> None:
    profile = resolve_risk_profile(
        _settings(tmp_path, market_family="btc_5m", stale_data_seconds=45, max_concurrent_positions=4)
    )
    # Explicit operator globals win over family defaults.
    assert profile.stale_data_seconds == 45
    assert profile.max_concurrent_positions == 4


def test_resolve_risk_profile_falls_back_to_globals_for_unknown_family(tmp_path: Path) -> None:
    profile = resolve_risk_profile(_settings(tmp_path, market_family="btc_daily_threshold"))
    assert isinstance(profile, RiskProfile)
    # No family override, so the global defaults are used verbatim.
    assert profile.stale_data_seconds == 30
    assert profile.max_concurrent_positions == 1


def test_risk_engine_rejects_on_max_concurrent_positions(tmp_path: Path) -> None:
    engine = RiskEngine(_settings(tmp_path, market_family="btc_5m"))
    state = _account_state(open_positions=1)
    risk = engine.evaluate(_snapshot(), _assessment(), state)
    assert not risk.approved
    assert "max_concurrent_positions" in risk.rejected_by


def test_risk_engine_allows_multiple_positions_for_btc_1h_profile(tmp_path: Path) -> None:
    engine = RiskEngine(_settings(tmp_path, market_family="btc_1h"))
    state = _account_state(open_positions=1)
    # Keep TTE well above the btc_1h buffer (180s) to isolate the concurrency check.
    risk = engine.evaluate(_snapshot(seconds_to_expiry=1200), _assessment(), state)
    assert risk.approved, risk.rejected_by


def test_risk_engine_rejects_when_correlation_cap_would_breach(tmp_path: Path) -> None:
    engine = RiskEngine(
        _settings(tmp_path, market_family="btc_1h", max_net_btc_exposure_usd=15.0, max_position_usd=10.0)
    )
    state = _account_state(open_positions=0, net_btc_exposure_usd=10.0)
    risk = engine.evaluate(_snapshot(), _assessment(side=SuggestedSide.YES), state)
    # Projected = 10 + 10 = 20 > 15 → blocked.
    assert not risk.approved
    assert "net_btc_exposure_cap" in risk.rejected_by


def test_risk_engine_allows_opposite_side_when_correlation_cap_offset(tmp_path: Path) -> None:
    engine = RiskEngine(
        _settings(tmp_path, market_family="btc_1h", max_net_btc_exposure_usd=15.0, max_position_usd=10.0)
    )
    state = _account_state(open_positions=0, net_btc_exposure_usd=10.0)
    risk = engine.evaluate(
        _snapshot(seconds_to_expiry=1200),
        _assessment(side=SuggestedSide.NO, edge=-0.05),
        state,
    )
    # Projected = 10 - 10 = 0 → within cap.
    assert risk.approved, risk.rejected_by


def test_risk_engine_dynamic_exit_buffer_uses_pct_for_short_horizon(tmp_path: Path) -> None:
    # btc_5m: pct=0.10 of 300s window → buffer=30s. TTE=25s ≤ 30 → rejected.
    engine = RiskEngine(_settings(tmp_path, market_family="btc_5m"))
    snapshot_at_edge = _snapshot(seconds_to_expiry=25)
    risk = engine.evaluate(snapshot_at_edge, _assessment(), _account_state())
    assert "expiry_buffer" in risk.rejected_by


def test_risk_engine_dynamic_exit_buffer_allows_longer_tte(tmp_path: Path) -> None:
    engine = RiskEngine(_settings(tmp_path, market_family="btc_5m"))
    # TTE=120s > 30s buffer → passes the expiry check.
    snapshot = _snapshot(seconds_to_expiry=120)
    risk = engine.evaluate(snapshot, _assessment(), _account_state())
    assert "expiry_buffer" not in risk.rejected_by


def test_risk_engine_dynamic_exit_buffer_scales_with_family_window(tmp_path: Path) -> None:
    # btc_1h: pct=0.05 of 3600s → buffer=180s. TTE=150 ≤ 180 → rejected;
    # TTE=300 > 180 → allowed.
    engine = RiskEngine(_settings(tmp_path, market_family="btc_1h"))
    near_expiry = engine.evaluate(_snapshot(seconds_to_expiry=150), _assessment(), _account_state())
    assert "expiry_buffer" in near_expiry.rejected_by
    far_expiry = engine.evaluate(_snapshot(seconds_to_expiry=300), _assessment(), _account_state())
    assert "expiry_buffer" not in far_expiry.rejected_by


def test_portfolio_exposure_summary_reflects_yes_and_no_positions(tmp_path: Path) -> None:
    portfolio = PortfolioEngine(tmp_path / "agent.db", starting_balance_usd=100.0)
    # Insert one YES and one NO position directly via record_live_fill.
    portfolio.record_live_fill(
        order_id="o1", market_id="m1", asset_id="token-yes", side=SuggestedSide.YES,
        fill_price=0.50, filled_size_shares=20.0,
    )
    portfolio.record_live_fill(
        order_id="o2", market_id="m2", asset_id="token-no", side=SuggestedSide.NO,
        fill_price=0.40, filled_size_shares=10.0,
    )
    exposure = portfolio.get_exposure_summary()
    assert exposure["long_btc_usd"] == 10.0  # 0.50 * 20
    assert exposure["short_btc_usd"] == 4.0  # 0.40 * 10
    assert exposure["net_btc_usd"] == 6.0
    assert exposure["total_exposure_usd"] == 14.0


def test_portfolio_account_state_carries_exposure_into_risk(tmp_path: Path) -> None:
    portfolio = PortfolioEngine(tmp_path / "agent.db", starting_balance_usd=100.0)
    portfolio.record_live_fill(
        order_id="o1", market_id="m1", asset_id="token-yes", side=SuggestedSide.YES,
        fill_price=0.50, filled_size_shares=20.0,
    )
    state = portfolio.get_account_state(ExecutionMode.LIVE)
    assert state.long_btc_exposure_usd == 10.0
    assert state.net_btc_exposure_usd == 10.0
