from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


class BtcState:
    """Rolling BTC price state driven by streaming trade/tick events.

    Maintains a fixed-size deque of (timestamp, price) samples and exposes
    log-returns over common horizons plus EWMA realized volatility. Designed to
    be updated in-place from either the websocket feed or a REST fallback, with
    no per-call network I/O.
    """

    def __init__(
        self,
        max_samples: int = 2048,
        vol_halflife_seconds: float = 300.0,
    ) -> None:
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=max_samples)
        self._vol_halflife_seconds = max(1.0, vol_halflife_seconds)
        self._ewma_var: float = 0.0
        self._ewma_initialized: bool = False

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

    def record(self, price: float, observed_at: datetime | None = None) -> None:
        if price <= 0.0 or not math.isfinite(price):
            return
        ts = observed_at or _utc_now()
        previous = self._samples[-1] if self._samples else None
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
        return BtcSnapshot(
            price=last_price,
            observed_at=self._samples[-1][0],
            log_return_10s=self.log_return_over(10.0, now=current_ts),
            log_return_1m=self.log_return_over(60.0, now=current_ts),
            log_return_5m=self.log_return_over(300.0, now=current_ts),
            log_return_15m=self.log_return_over(900.0, now=current_ts),
            realized_vol_30m=self.realized_vol(1800.0),
            sample_count=len(self._samples),
        )

    def seconds_since_last_update(self, now: datetime | None = None) -> float:
        if not self._samples:
            return math.inf
        current_ts = now or _utc_now()
        return max(0.0, (current_ts - self._samples[-1][0]).total_seconds())
