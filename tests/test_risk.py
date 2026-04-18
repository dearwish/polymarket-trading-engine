from datetime import timedelta

from polymarket_ai_agent.config import Settings
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


def build_snapshot(
    spread: float = 0.01,
    depth: float = 500.0,
    seconds_to_expiry: int = 120,
    observed_age_seconds: int = 0,
    collected_age_seconds: int = 0,
) -> MarketSnapshot:
    candidate = MarketCandidate(
        market_id="1",
        question="Will BTC be up in 5 minutes?",
        condition_id="cond-1",
        slug="btc-5m",
        end_date_iso="2099-01-01T00:00:00Z",
        yes_token_id="yes",
        no_token_id="no",
        implied_probability=0.52,
        liquidity_usd=10000.0,
        volume_24h_usd=20000.0,
    )
    return MarketSnapshot(
        candidate=candidate,
        orderbook=OrderBookSnapshot(
            bid=0.51,
            ask=0.52,
            midpoint=0.515,
            spread=spread,
            depth_usd=depth,
            last_trade_price=0.515,
            observed_at=utc_now() - timedelta(seconds=observed_age_seconds),
        ),
        seconds_to_expiry=seconds_to_expiry,
        recent_price_change_bps=10.0,
        recent_trade_count=5,
        external_price=100000.0,
        collected_at=utc_now() - timedelta(seconds=collected_age_seconds),
    )


def build_assessment(edge: float = 0.05, confidence: float = 0.9) -> MarketAssessment:
    return MarketAssessment(
        market_id="1",
        fair_probability=0.57,
        confidence=confidence,
        suggested_side=SuggestedSide.YES,
        expiry_risk="LOW",
        reasons_for_trade=["edge exists"],
        reasons_to_abstain=[],
        edge=edge,
        raw_model_output="{}",
    )


def test_risk_rejects_wide_spread() -> None:
    settings = Settings(max_spread=0.02)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    risk = engine.evaluate(build_snapshot(spread=0.05), build_assessment(), state)
    assert not risk.approved
    assert "spread_limit" in risk.rejected_by


def test_risk_approves_clean_setup() -> None:
    # Default market_family is btc_1h → exit buffer = 0.05 * 3600 = 180s.
    # Keep TTE well above that so only the generic gates apply.
    settings = Settings()
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    decision = engine.decide_trade(build_snapshot(seconds_to_expiry=1200), build_assessment(), state)
    assert decision.status.value == "APPROVED"
    assert decision.side.value == "YES"


def test_risk_rejects_low_confidence() -> None:
    settings = Settings(min_confidence=0.8)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    risk = engine.evaluate(build_snapshot(), build_assessment(confidence=0.5), state)
    assert not risk.approved
    assert "confidence_limit" in risk.rejected_by


def test_risk_rejects_daily_loss_limit() -> None:
    settings = Settings(max_daily_loss_usd=5.0)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=-5.0)
    risk = engine.evaluate(build_snapshot(), build_assessment(), state)
    assert not risk.approved
    assert "daily_loss_limit" in risk.rejected_by


def test_risk_rejects_stale_data() -> None:
    settings = Settings(stale_data_seconds=30)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    risk = engine.evaluate(
        build_snapshot(observed_age_seconds=45, collected_age_seconds=45),
        build_assessment(),
        state,
    )
    assert not risk.approved
    assert "stale_data" in risk.rejected_by
