"""Two-sided quote pricing for the market-maker strategy.

The quoter takes the current book mid, our target half-spread, and an
inventory-skew signal, and returns the YES-buy + NO-buy limit prices we
should rest at. Both legs are BUY orders (Polymarket binary markets only
accept BUY+SELL on existing inventory; expressing "sell YES" as "buy NO"
keeps the strategy in BUY-only mode and avoids the SELL-side reconciliation
plumbing).

Conventions:

- ``mid``: the YES-side midpoint, i.e. ``(bid_yes + ask_yes) / 2``.
- A YES-buy at ``yes_bid_quote`` competes with the existing YES bid book.
- A NO-buy at ``no_bid_quote`` competes with the existing NO bid book.
  Equivalently, ``no_bid_quote = 1 − yes_ask_quote`` — buying NO at 0.45
  is the same as offering to sell YES at 0.55.
- The spread we quote is symmetric around mid: a target half-spread of
  0.02 means the YES-buy sits at ``mid − 0.02`` and the NO-buy sits at
  ``(1 − mid) − 0.02``. If both fill we book ``1 − (yes_bid + no_bid) =
  2 × half_spread`` of gross P&L.

Inventory skew shifts both legs in the same direction: a positive YES
inventory (we've already bought too much YES) makes us:

- Less aggressive on YES-buys → ``yes_bid_quote`` shifts DOWN.
- More aggressive on NO-buys → ``no_bid_quote`` shifts UP (a higher NO bid
  ≡ a lower implied YES ask, which means we sell YES sooner).

The ``skew`` input is a signed normalised inventory in the range [-1, 1]
(see :func:`compute_inventory`). At skew = +1 we apply
``skew_strength × half_spread`` of one-sided pressure; at skew = -1 the
mirror.

Reward-band gating is the caller's responsibility: a quote that falls
outside Polymarket's ``±max_spread/100`` reward band still earns the
spread but loses the daily subsidy. The scorer reads the candidate's
``rewards_max_spread_pct`` and decides whether to abstain on a given side
based on the prices this function returns.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class QuotePair:
    """Per-tick output of the quoter — the two prices we'd rest at right now.

    A ``None`` on either leg means "skip this side this tick" (book too
    thin, computed price out of the ``[0.01, 0.99]`` band, or the leg is
    halted because inventory hit the cap). The caller is expected to
    place / cancel each leg independently.
    """

    yes_bid: float | None
    no_bid: float | None
    mid_yes: float
    half_spread: float
    skew: float


def compute_quote_pair(
    *,
    bid_yes: float,
    ask_yes: float,
    half_spread: float,
    skew: float,
    skew_strength: float,
    halt_yes_buy: bool = False,
    halt_no_buy: bool = False,
    min_price: float = 0.01,
    max_price: float = 0.99,
) -> QuotePair:
    """Compute the YES-buy and NO-buy limit prices for one tick.

    Returns ``QuotePair`` with ``yes_bid`` / ``no_bid`` populated when the
    book supports a quote on that side, or ``None`` to signal "skip".

    The quoter abstains entirely (both ``None``) when the book is missing
    a side, crossed, or the resulting half-spread would inevitably push
    one of the prices outside ``[min_price, max_price]``. Callers should
    treat that as "do nothing this tick".
    """
    # Refuse to quote on a one-sided / crossed book — without a real ask
    # we can't compute a mid, and a quote based only on bid would be a
    # directional bet, not market making.
    if bid_yes <= 0.0 or ask_yes <= 0.0 or bid_yes >= ask_yes:
        return QuotePair(
            yes_bid=None,
            no_bid=None,
            mid_yes=0.0,
            half_spread=half_spread,
            skew=skew,
        )

    mid_yes = (bid_yes + ask_yes) / 2.0
    # Clamp skew to the unit interval so a runaway position can't push
    # quotes off the price grid. ``skew_strength`` is the operator-tunable
    # multiplier on the half-spread.
    bounded_skew = max(-1.0, min(1.0, skew))
    skew_offset = bounded_skew * skew_strength * half_spread

    yes_bid_raw = mid_yes - half_spread - skew_offset
    no_bid_raw = (1.0 - mid_yes) - half_spread + skew_offset

    yes_bid = _clamp(yes_bid_raw, min_price, max_price)
    no_bid = _clamp(no_bid_raw, min_price, max_price)

    # If the clamp materially distorted the price (e.g. mid=0.05 with
    # half_spread=0.04 → raw yes_bid=0.01 floor) we still post — the leg
    # at the floor just earns less spread. We return None only when the
    # leg is explicitly halted by inventory caps.
    return QuotePair(
        yes_bid=None if halt_yes_buy else yes_bid,
        no_bid=None if halt_no_buy else no_bid,
        mid_yes=mid_yes,
        half_spread=half_spread,
        skew=bounded_skew,
    )


def in_reward_band(
    quote_price: float,
    midpoint: float,
    rewards_max_spread_pct: float,
) -> bool:
    """True when ``quote_price`` sits inside Polymarket's daily reward band.

    The reward formula in :mod:`engine.maker_rewards` zeros out any quote
    farther than ``rewards_max_spread_pct/100`` from mid. Mirroring that
    check here lets the scorer abstain on a leg whose price would drift
    out of the band — there's no point posting an MM quote that earns
    only the spread when the strategy's selection thesis depends on the
    daily subsidy.

    Returns ``True`` when ``rewards_max_spread_pct <= 0`` (no reward band
    configured = "every quote qualifies"); the caller decides whether to
    abstain on markets that pay no rewards via a separate flag.
    """
    if rewards_max_spread_pct <= 0.0:
        return True
    band_half_width = rewards_max_spread_pct / 100.0
    return abs(quote_price - midpoint) <= band_half_width + 1e-9


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
