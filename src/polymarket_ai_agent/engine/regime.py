"""Coarse BTC regime classifier.

Phase 1 of the adaptive-regime branch. Produces a single label per tick from
features already carried on the EvidencePacket — HTF log returns and 30-minute
realized volatility — so downstream strategy selection (Phase 2+) has a
stable, cheap signal to condition on.

Instrumentation-only in phase 1: the label is logged on ``daemon_tick`` but
no scorer or gate consumes it yet. That keeps the classifier thresholds
tunable from soak data before any trading decision depends on them.

Labels:
  TRENDING_UP    — 1h and 4h log returns both positive and |4h| ≥ trend_min
  TRENDING_DOWN  — mirror of TRENDING_UP
  HIGH_VOL       — realized_vol_30m exceeds vol_high_threshold (dominates
                   trend labels; chop is the primary risk even with a drift)
  RANGING        — neither trend nor high vol triggered
  UNKNOWN        — insufficient HTF buffer (both returns are zero, which
                   means the 1-minute bar buffer hasn't warmed up)

Thresholds mirror the existing scorer gates so the classifier and the gates
read the same "market is trending" story without two sources of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from polymarket_ai_agent.types import EvidencePacket


class Regime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    HIGH_VOL = "HIGH_VOL"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True, frozen=True)
class RegimeThresholds:
    """Thresholds for regime classification.

    ``trend_min_abs_4h`` mirrors ``quant_trend_filter_min_abs_return`` applied
    to the 4h window: a trend strong enough to matter must have moved at
    least this much. The 1h return's sign must agree, which filters out
    fresh reversals where 4h still shows the old direction.

    ``vol_high`` mirrors ``quant_vol_regime_high_threshold``: once realized
    volatility clears this line, the regime is "chop" regardless of any
    apparent HTF drift.
    """

    trend_min_abs_4h: float = 0.003
    vol_high: float = 0.005


def classify_regime(
    packet: EvidencePacket,
    thresholds: RegimeThresholds | None = None,
) -> Regime:
    """Return the coarse regime label for ``packet``.

    Deterministic and side-effect-free — safe to call on every tick without
    worrying about I/O. Order of checks matters: HIGH_VOL is checked before
    trend so a volatile rally labels as HIGH_VOL (scorer should sit out)
    rather than TRENDING_UP (scorer might size up).
    """
    t = thresholds or RegimeThresholds()
    vol = float(packet.realized_vol_30m)
    r1h = float(packet.btc_log_return_1h)
    r4h = float(packet.btc_log_return_4h)

    if r1h == 0.0 and r4h == 0.0:
        return Regime.UNKNOWN
    if vol >= t.vol_high:
        return Regime.HIGH_VOL
    if abs(r4h) >= t.trend_min_abs_4h and (r1h * r4h) > 0.0:
        return Regime.TRENDING_UP if r4h > 0.0 else Regime.TRENDING_DOWN
    return Regime.RANGING
