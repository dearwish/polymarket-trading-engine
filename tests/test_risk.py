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


def test_risk_rejects_entry_below_min_tte() -> None:
    settings = Settings(min_entry_tte_seconds=90)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    risk = engine.evaluate(build_snapshot(seconds_to_expiry=60), build_assessment(), state)
    assert not risk.approved
    assert "min_entry_tte" in risk.rejected_by


def test_risk_approves_when_min_tte_disabled() -> None:
    # With min_entry_tte_seconds=0 a 60s-TTE snapshot must not be flagged.
    settings = Settings(min_entry_tte_seconds=0, market_family="btc_5m")
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    risk = engine.evaluate(build_snapshot(seconds_to_expiry=200), build_assessment(), state)
    assert "min_entry_tte" not in risk.rejected_by


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


def _snapshot_with_asks(ask_yes: float, ask_no: float, **kw) -> MarketSnapshot:
    """Variant of build_snapshot that lets a test pin both YES and NO asks
    explicitly. Used by the entry-price-gate tests so the chosen-side ask
    is unambiguous."""
    snap = build_snapshot(**kw)
    book = snap.orderbook
    snap.orderbook = OrderBookSnapshot(
        bid=book.bid,
        ask=ask_yes,
        midpoint=book.midpoint,
        spread=book.spread,
        depth_usd=book.depth_usd,
        last_trade_price=book.last_trade_price,
        two_sided=book.two_sided,
        observed_at=book.observed_at,
        bid_no=1.0 - ask_yes,
        ask_no=ask_no,
    )
    return snap


def test_risk_rejects_below_min_entry_price_yes_side() -> None:
    """YES trade with ask below floor → min_entry_price reject."""
    settings = Settings(quant_min_entry_price=0.20, max_spread=0.05)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    snap = _snapshot_with_asks(ask_yes=0.10, ask_no=0.92)
    risk = engine.evaluate(snap, build_assessment(), state)
    assert not risk.approved
    assert "min_entry_price" in risk.rejected_by


def test_risk_rejects_below_min_entry_price_no_side() -> None:
    """NO trade with NO-side ask below floor → min_entry_price reject."""
    settings = Settings(quant_min_entry_price=0.20, max_spread=0.05)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    snap = _snapshot_with_asks(ask_yes=0.92, ask_no=0.10)
    no_assessment = MarketAssessment(
        market_id="1", fair_probability=0.30, confidence=0.9,
        suggested_side=SuggestedSide.NO, expiry_risk="LOW",
        reasons_for_trade=[], reasons_to_abstain=[], edge=0.05, raw_model_output="{}",
    )
    risk = engine.evaluate(snap, no_assessment, state)
    assert not risk.approved
    assert "min_entry_price" in risk.rejected_by


def test_risk_rejects_above_max_entry_price() -> None:
    """Mid-band entries (ask above ceiling) → max_entry_price reject."""
    settings = Settings(quant_max_entry_price=0.50, max_spread=0.05)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    snap = _snapshot_with_asks(ask_yes=0.62, ask_no=0.40)
    risk = engine.evaluate(snap, build_assessment(), state)
    assert not risk.approved
    assert "max_entry_price" in risk.rejected_by


def test_risk_min_entry_price_disabled_when_zero() -> None:
    """Floor=0 (default) → no min_entry_price reject even at distressed prices."""
    settings = Settings(quant_min_entry_price=0.0, max_spread=0.05)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    snap = _snapshot_with_asks(ask_yes=0.05, ask_no=0.97)
    risk = engine.evaluate(snap, build_assessment(), state)
    assert "min_entry_price" not in risk.rejected_by


def test_risk_max_entry_price_disabled_when_zero() -> None:
    """Ceiling=0 (default) → no max_entry_price reject even at high prices."""
    settings = Settings(quant_max_entry_price=0.0, max_spread=0.05)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    snap = _snapshot_with_asks(ask_yes=0.95, ask_no=0.07)
    risk = engine.evaluate(snap, build_assessment(), state)
    assert "max_entry_price" not in risk.rejected_by


def test_risk_entry_price_gates_skip_when_ask_zero() -> None:
    """Snapshots with default ask=0 (legacy callers / test fixtures) must
    NOT trip either gate — the ``ask > 0`` guard short-circuits.
    """
    settings = Settings(quant_min_entry_price=0.32, quant_max_entry_price=0.50, max_spread=0.05)
    engine = RiskEngine(settings)
    state = AccountState(mode=ExecutionMode.PAPER, available_usd=100.0, open_positions=0, daily_realized_pnl=0.0)
    no_assessment = MarketAssessment(
        market_id="1", fair_probability=0.30, confidence=0.9,
        suggested_side=SuggestedSide.NO, expiry_risk="LOW",
        reasons_for_trade=[], reasons_to_abstain=[], edge=0.05, raw_model_output="{}",
    )
    # build_snapshot leaves ask_no=0.0 (default) — the NO trade must not be
    # rejected by the price gates.
    risk = engine.evaluate(build_snapshot(), no_assessment, state)
    assert "min_entry_price" not in risk.rejected_by
    assert "max_entry_price" not in risk.rejected_by
