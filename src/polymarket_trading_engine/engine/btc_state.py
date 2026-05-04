from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def session_bucket(observed_at: datetime) -> str:
    """Map a UTC timestamp to a coarse session tag.

    Asia 00:00–07:59 UTC, EU 08:00–12:59, US 13:00–20:59, Off 21:00–23:59.
    Used as a regime/time-of-day feature alongside HTF indicators; logged in
    daemon_tick so analyze_soak can stratify hit-rate / Brier by session
    before any scorer change.
    """
    hour = observed_at.astimezone(timezone.utc).hour
    if hour < 8:
        return "asia"
    if hour < 13:
        return "eu"
    if hour < 21:
        return "us"
    return "off"


@dataclass(slots=True)
class BtcSnapshot:
    price: float
    observed_at: datetime
    log_return_10s: float
    log_return_1m: float
    log_return_5m: float
    log_return_15m: float
    realized_vol_30m: float
    sample_count: int
    btc_session: str = "off"
    # Short-horizon BTC delta. Used by overreaction-fade to detect
    # "BTC is moving NOW" — the 5m window is too smoothed to catch a
    # market in free-fall, which is exactly when adaptive_v2's claimed
    # overreaction is in fact a real move (mkt 2068470, 2026-04-25).
    # Defaults to 0.0 so existing test fixtures that build BtcSnapshot
    # positionally don't break.
    log_return_30s: float = 0.0
    # HTF log returns derived from the 1-minute bar buffer (backfilled from
    # Binance /klines on startup, then rolled forward as ticks cross minute
    # boundaries). Emit 0.0 until enough bars have accumulated for the horizon.
    btc_log_return_1h: float = 0.0
    btc_log_return_4h: float = 0.0
    btc_log_return_24h: float = 0.0
    minute_bar_count: int = 0


class BtcState:
    """Rolling BTC price state driven by streaming trade/tick events.

    Maintains a fixed-size deque of (timestamp, price) samples and exposes
    log-returns over common horizons plus EWMA realized volatility. Designed to
    be updated in-place from either the websocket feed or a REST fallback, with
    no per-call network I/O.
    """

    def __init__(
        self,
        max_samples: int = 8192,
        vol_halflife_seconds: float = 300.0,
        min_record_interval_seconds: float = 1.0,
        max_minute_bars: int = 1500,
    ) -> None:
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=max_samples)
        self._vol_halflife_seconds = max(1.0, vol_halflife_seconds)
        # Decimate incoming ticks so the deque covers meaningful TIME rather than
        # just the most recent few seconds. At Binance's ~70 Hz raw feed a 2048
        # deque used to hold only ~30s of history, which made longer-horizon
        # look-ups (e.g. log-return since a 15-min candle opened) return ~0.
        # With 1s decimation + 8192 slots we retain ~2.3h of BTC history.
        self._min_record_interval_seconds = max(0.0, min_record_interval_seconds)
        self._ewma_var: float = 0.0
        self._ewma_initialized: bool = False
        # 1-minute bar buffer for HTF indicators. Seeded from REST klines on
        # startup and rolled forward in-place as tick minutes advance.
        #   (minute_open_ts_utc, close_price, base_asset_volume)
        self._minute_bars: deque[tuple[datetime, float, float]] = deque(maxlen=max_minute_bars)
        # Accumulator for the "current" (still-open) minute. We append to the
        # deque only once the tick stream crosses into the next minute, so the
        # deque always reflects FINALISED bars.
        self._current_minute_ts: datetime | None = None
        self._current_minute_close: float = 0.0
        self._current_minute_volume: float = 0.0

    @property
    def last_price(self) -> float | None:
        if not self._samples:
            return None
        return self._samples[-1][1]

    @property
    def last_observed_at(self) -> datetime | None:
        if not self._samples:
            return None
        return self._samples[-1][0]

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def record(
        self,
        price: float,
        observed_at: datetime | None = None,
        quantity: float = 0.0,
    ) -> None:
        if price <= 0.0 or not math.isfinite(price):
            return
        ts = observed_at or _utc_now()
        # Minute-bar accumulator: every tick contributes to the current minute
        # regardless of decimation. This keeps HTF features accurate even when
        # the high-frequency tick deque decimates most ticks away.
        self._advance_minute_bar(ts, float(price), max(0.0, float(quantity)))
        previous = self._samples[-1] if self._samples else None
        if previous is not None:
            dt = (ts - previous[0]).total_seconds()
            # Decimate: skip ticks that arrive closer than min_record_interval.
            # We still update EWMA vol on skipped ticks below so variance stays
            # responsive, but the deque itself only grows at the decimation rate.
            if dt < self._min_record_interval_seconds:
                return
        self._samples.append((ts, float(price)))
        if previous is None:
            return
        prev_ts, prev_price = previous
        dt = (ts - prev_ts).total_seconds()
        if dt <= 0.0 or prev_price <= 0.0:
            return
        log_return = math.log(price / prev_price)
        per_second_sq = log_return * log_return / dt
        decay = math.exp(-math.log(2.0) * dt / self._vol_halflife_seconds)
        if not self._ewma_initialized:
            self._ewma_var = per_second_sq
            self._ewma_initialized = True
        else:
            self._ewma_var = decay * self._ewma_var + (1.0 - decay) * per_second_sq

    def _minute_floor(self, ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def _advance_minute_bar(self, ts: datetime, price: float, quantity: float) -> None:
        """Update the minute-bar accumulator and flush on minute rollover.

        We push the PREVIOUS minute's finalised bar into the deque only once a
        tick arrives whose floor-minute is strictly greater than the
        accumulator's. Ticks that go backwards in time (ws reconnect) are
        ignored for the accumulator to keep ordering monotonic.
        """
        minute_ts = self._minute_floor(ts)
        if self._current_minute_ts is None:
            self._current_minute_ts = minute_ts
            self._current_minute_close = price
            self._current_minute_volume = quantity
            return
        if minute_ts < self._current_minute_ts:
            # Out-of-order tick — ignore for bar accumulation, EWMA still runs.
            return
        if minute_ts == self._current_minute_ts:
            self._current_minute_close = price
            self._current_minute_volume += quantity
            return
        # New minute: finalise the previous one and start fresh.
        self._minute_bars.append(
            (self._current_minute_ts, self._current_minute_close, self._current_minute_volume)
        )
        self._current_minute_ts = minute_ts
        self._current_minute_close = price
        self._current_minute_volume = quantity

    def backfill_minute_bars(self, bars: list[tuple[datetime, float, float]]) -> int:
        """Seed the minute-bar deque with historical 1-min klines.

        Existing bars are discarded. Returns the count of bars retained (which
        may be less than ``len(bars)`` if the deque ``maxlen`` clips the head).
        """
        self._minute_bars.clear()
        # Trust the REST source for ordering. Drop malformed rows and ensure
        # each datetime is UTC-aware and minute-floored.
        for ts, close, volume in bars:
            if close <= 0.0 or volume < 0.0:
                continue
            floored = self._minute_floor(ts.astimezone(timezone.utc))
            self._minute_bars.append((floored, float(close), float(volume)))
        return len(self._minute_bars)

    @property
    def minute_bar_count(self) -> int:
        """Finalised bars plus the currently-accumulating bar if any."""
        return len(self._minute_bars) + (1 if self._current_minute_ts is not None else 0)

    def log_return_over_minutes(self, minutes: int, now: datetime | None = None) -> float:
        """Rolling log-return over ``minutes`` minutes using the minute-bar buffer.

        Prefers ``_minute_bars`` over the tick deque because the latter only
        covers ~2.3h whereas HTF indicators need up to 24h of lookback. Falls
        back to the tick deque when the bar buffer is empty (cold start).
        """
        if minutes <= 0:
            return 0.0
        # Current price = current accumulator close if live, else last finalised bar.
        if self._current_minute_ts is not None and self._current_minute_close > 0.0:
            current_price = self._current_minute_close
        elif self._minute_bars:
            current_price = self._minute_bars[-1][1]
        else:
            return self.log_return_over(float(minutes) * 60.0, now=now)
        current_ts = now or self._current_minute_ts or self._minute_bars[-1][0]
        target_ts = current_ts.timestamp() - minutes * 60.0
        reference_price: float | None = None
        for ts, close, _vol in reversed(self._minute_bars):
            if ts.timestamp() <= target_ts:
                reference_price = close
                break
        if reference_price is None:
            # Not enough history yet.
            return 0.0
        if reference_price <= 0.0 or current_price <= 0.0:
            return 0.0
        return math.log(current_price / reference_price)

    def log_return_over(self, seconds: float, now: datetime | None = None) -> float:
        if seconds <= 0.0 or not self._samples:
            return 0.0
        current_ts = now or _utc_now()
        current_price = self._samples[-1][1]
        target = current_ts.timestamp() - seconds
        reference_price: float | None = None
        for ts, price in reversed(self._samples):
            if ts.timestamp() <= target:
                reference_price = price
                break
        if reference_price is None:
            reference_price = self._samples[0][1]
        if reference_price <= 0.0 or current_price <= 0.0:
            return 0.0
        return math.log(current_price / reference_price)

    def realized_vol(self, horizon_seconds: float = 1800.0) -> float:
        if not self._ewma_initialized:
            return 0.0
        # EWMA variance is per-second; scale to the requested horizon.
        return math.sqrt(max(self._ewma_var, 0.0) * max(horizon_seconds, 0.0))

    def snapshot(self, now: datetime | None = None) -> BtcSnapshot | None:
        if not self._samples:
            return None
        current_ts = now or _utc_now()
        last_price = self._samples[-1][1]
        observed_at = self._samples[-1][0]
        return BtcSnapshot(
            price=last_price,
            observed_at=observed_at,
            log_return_10s=self.log_return_over(10.0, now=current_ts),
            log_return_30s=self.log_return_over(30.0, now=current_ts),
            log_return_1m=self.log_return_over(60.0, now=current_ts),
            log_return_5m=self.log_return_over(300.0, now=current_ts),
            log_return_15m=self.log_return_over(900.0, now=current_ts),
            realized_vol_30m=self.realized_vol(1800.0),
            sample_count=len(self._samples),
            btc_session=session_bucket(observed_at),
            btc_log_return_1h=self.log_return_over_minutes(60, now=current_ts),
            btc_log_return_4h=self.log_return_over_minutes(240, now=current_ts),
            btc_log_return_24h=self.log_return_over_minutes(1440, now=current_ts),
            minute_bar_count=self.minute_bar_count,
        )

    def seconds_since_last_update(self, now: datetime | None = None) -> float:
        if not self._samples:
            return math.inf
        current_ts = now or _utc_now()
        return max(0.0, (current_ts - self._samples[-1][0]).total_seconds())
