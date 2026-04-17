from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from polymarket_ai_agent.engine.btc_state import BtcState


def _ts(seconds: float) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_btc_state_records_returns_over_horizons() -> None:
    state = BtcState(max_samples=512, vol_halflife_seconds=60.0)
    state.record(100.0, _ts(0.0))
    state.record(101.0, _ts(10.0))
    state.record(102.0, _ts(70.0))
    state.record(103.0, _ts(310.0))

    assert state.last_price == 103.0
    assert state.sample_count == 4

    log_return_10s = state.log_return_over(10.0, now=_ts(310.0))
    assert log_return_10s == 0.0 or log_return_10s < 0.05
    assert state.log_return_over(300.0, now=_ts(310.0)) > 0.0
    assert state.log_return_over(3600.0, now=_ts(310.0)) > 0.0


def test_btc_state_realized_vol_non_negative() -> None:
    state = BtcState(max_samples=128, vol_halflife_seconds=30.0)
    base = 100.0
    for idx in range(60):
        # Alternating returns keep EWMA variance strictly positive.
        bump = 0.1 if idx % 2 == 0 else -0.1
        base *= math.exp(bump * 0.01)
        state.record(base, _ts(idx))
    vol = state.realized_vol(60.0)
    assert vol > 0.0
    assert math.isfinite(vol)


def test_btc_state_ignores_bad_prices() -> None:
    state = BtcState()
    state.record(0.0)
    state.record(float("nan"))
    assert state.sample_count == 0
    assert state.last_price is None


def test_btc_state_snapshot_exposes_all_horizons() -> None:
    state = BtcState()
    state.record(100.0, _ts(0.0))
    state.record(100.5, _ts(5.0))
    state.record(101.0, _ts(65.0))
    snapshot = state.snapshot(now=_ts(65.0))
    assert snapshot is not None
    assert snapshot.price == 101.0
    assert snapshot.sample_count == 3
    assert snapshot.log_return_10s == 0.0 or snapshot.log_return_10s > 0.0
    assert snapshot.log_return_1m > 0.0


def test_btc_state_seconds_since_last_update() -> None:
    state = BtcState()
    state.record(100.0, _ts(0.0))
    assert state.seconds_since_last_update(now=_ts(12.5)) == 12.5
