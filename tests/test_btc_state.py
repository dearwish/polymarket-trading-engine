from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from polymarket_ai_agent.engine.btc_state import BtcState, session_bucket


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


def test_btc_state_decimates_sub_interval_ticks() -> None:
    """High-frequency ticks (< min_record_interval_seconds apart) must be
    dropped so the deque covers TIME not just the last N milliseconds."""
    state = BtcState(max_samples=64, min_record_interval_seconds=1.0)
    base = _ts(0.0)
    # 10 ticks spaced 100ms apart within the same 1s window: only the first
    # should be retained.
    for i in range(10):
        state.record(100.0 + i, base + timedelta(seconds=i * 0.1))
    assert state.sample_count == 1
    # A tick 1.5s later is past the interval → kept.
    state.record(200.0, base + timedelta(seconds=1.5))
    assert state.sample_count == 2
    assert state.last_price == 200.0


def test_btc_state_seconds_since_last_update() -> None:
    state = BtcState()
    state.record(100.0, _ts(0.0))
    assert state.seconds_since_last_update(now=_ts(12.5)) == 12.5


def test_session_bucketing_across_utc_hours() -> None:
    """UTC-hour → session mapping covers every hour and the 4 boundary flips."""
    def at(hour: int) -> datetime:
        return datetime(2026, 4, 19, hour, 30, 0, tzinfo=timezone.utc)

    # Asia: 00:00–07:59
    for h in range(0, 8):
        assert session_bucket(at(h)) == "asia", f"hour {h} should be asia"
    # EU: 08:00–12:59
    for h in range(8, 13):
        assert session_bucket(at(h)) == "eu", f"hour {h} should be eu"
    # US: 13:00–20:59
    for h in range(13, 21):
        assert session_bucket(at(h)) == "us", f"hour {h} should be us"
    # Off: 21:00–23:59
    for h in range(21, 24):
        assert session_bucket(at(h)) == "off", f"hour {h} should be off"


def test_session_bucket_normalises_non_utc_timestamps() -> None:
    """A timestamp carrying a non-UTC offset must be converted before bucketing."""
    # 2026-04-19 03:30 +05:00 = 2026-04-19 22:30 UTC → "off".
    from datetime import timezone as tz
    non_utc = datetime(2026, 4, 19, 3, 30, tzinfo=tz(timedelta(hours=5)))
    assert session_bucket(non_utc) == "off"


def test_snapshot_populates_btc_session() -> None:
    state = BtcState()
    # Pick a timestamp that falls into the US session.
    us_noon = datetime(2026, 4, 19, 15, 0, 0, tzinfo=timezone.utc)
    state.record(100.0, us_noon)
    state.record(101.0, us_noon + timedelta(seconds=5))
    snapshot = state.snapshot(now=us_noon + timedelta(seconds=5))
    assert snapshot is not None
    assert snapshot.btc_session == "us"


def test_backfill_minute_bars_populates_buffer() -> None:
    state = BtcState(max_minute_bars=10)
    base = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    bars = [(base + timedelta(minutes=i), 100.0 + i, 1.5) for i in range(5)]
    retained = state.backfill_minute_bars(bars)
    assert retained == 5
    assert state.minute_bar_count == 5  # no accumulator yet


def test_backfill_minute_bars_drops_malformed_rows() -> None:
    state = BtcState()
    base = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    bars = [
        (base, 100.0, 1.0),
        (base + timedelta(minutes=1), 0.0, 1.0),    # bad close
        (base + timedelta(minutes=2), 101.0, -0.5), # bad volume
        (base + timedelta(minutes=3), 102.0, 2.0),
    ]
    assert state.backfill_minute_bars(bars) == 2


def test_minute_bars_roll_on_minute_boundary() -> None:
    state = BtcState()
    base = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    state.record(100.0, base + timedelta(seconds=1), quantity=0.5)
    state.record(101.0, base + timedelta(seconds=30), quantity=0.25)
    # Still within the same minute — nothing finalised yet.
    assert len(state._minute_bars) == 0
    # Cross into the next minute — the previous minute gets flushed.
    state.record(102.0, base + timedelta(minutes=1, seconds=5), quantity=1.0)
    assert len(state._minute_bars) == 1
    ts, close, vol = state._minute_bars[0]
    assert ts == base
    assert close == 101.0  # last tick within that minute
    assert vol == 0.75     # accumulated aggTrade quantities


def test_minute_bars_ignore_out_of_order_ticks() -> None:
    state = BtcState()
    base = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    state.record(100.0, base + timedelta(minutes=1), quantity=1.0)
    # Tick from 5 minutes earlier — should not corrupt the accumulator.
    state.record(999.0, base - timedelta(minutes=5), quantity=1.0)
    # Next-minute tick flushes the (still intact) 12:01 bar.
    state.record(102.0, base + timedelta(minutes=2), quantity=0.5)
    assert len(state._minute_bars) == 1
    ts, close, _ = state._minute_bars[0]
    assert ts == base + timedelta(minutes=1)
    assert close == 100.0


def test_log_return_over_minutes_uses_minute_bar_buffer() -> None:
    state = BtcState()
    base = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    # 4 hours of bars with a clean +1% drift each hour.
    bars = []
    for i in range(4 * 60 + 1):
        bars.append((base + timedelta(minutes=i), 100.0 * math.exp(i * 0.0001), 1.0))
    state.backfill_minute_bars(bars)
    # Register a live tick to anchor "now" as a minute accumulator.
    now = base + timedelta(minutes=4 * 60)
    state.record(100.0 * math.exp(4 * 60 * 0.0001), now, quantity=0.0)
    r1h = state.log_return_over_minutes(60, now=now)
    r4h = state.log_return_over_minutes(240, now=now)
    # 1h ≈ 60 * 0.0001 = 0.006, 4h ≈ 240 * 0.0001 = 0.024.
    assert abs(r1h - 0.006) < 1e-4
    assert abs(r4h - 0.024) < 1e-4


def test_snapshot_tolerates_cold_minute_buffer() -> None:
    """HTF fields emit 0.0 until enough minute bars have accumulated."""
    state = BtcState()
    state.record(100.0, _ts(0.0))
    state.record(101.0, _ts(5.0))
    snapshot = state.snapshot(now=_ts(5.0))
    assert snapshot is not None
    # With ~5s of history and no kline backfill, all HTF returns should be 0.
    assert snapshot.btc_log_return_1h == 0.0
    assert snapshot.btc_log_return_4h == 0.0
    assert snapshot.btc_log_return_24h == 0.0
