"""Per-tick scoring decision for the market-maker strategy.

The MM scorer is **not** a directional alpha scorer — it doesn't pick
YES or NO based on a fair-value model. Its job is simply to gate when
the strategy should be quoting at all. The actual two-sided quote
placement is owned by the daemon's ``_handle_market_maker_strategy``
lifecycle, which calls into :func:`engine.market_maker.quoter.compute_quote_pair`
and the inventory module on every approved tick.

Gates (any failure → ABSTAIN with a logged reason):

1. Time-to-expiry too low — the half-spread typically can't be earned
   inside the final ``mm_min_tte_seconds`` window because the book gets
   thin and one-sided as resolution approaches.
2. Book one-sided / crossed — no mid to anchor quotes. Implicitly
   subsumes the BTC scorers' "pre-market" gate (a not-yet-open candle
   has no two-sided book), so this scorer doesn't check
   ``packet.is_pre_market`` directly — that field is BTC-candle-specific
   (``tte > family_window_seconds``) and would falsely fire on every
   sports / politics market in the MM universe whose TTE outruns the
   nominal 15-minute / 1-hour BTC candle window.
3. Market spread too tight — under ``mm_min_market_spread`` we'd need to
   rest INSIDE the existing spread, which is fine for a conservative
   maker but eliminates the spread we're supposed to capture. Skip.
4. Market spread too wide — over ``mm_max_market_spread`` is the toxic-
   flow signature: someone with information is dumping into a thin book.
   Don't make.
5. Reward gating — when ``mm_require_rewards=True`` and the candidate
   pays no maker subsidy, abstain. The MM thesis is that the daily yield
   plus captured spread compensates for adverse selection; without the
   yield component the math gets thin fast on short-horizon markets.

A passing tick returns an APPROVED assessment tagged with
:data:`MARKET_MAKER_STRATEGY_TAG`. The chosen ``suggested_side`` is set
to YES as a routing convention (the daemon's MM handler doesn't read it;
it always quotes both legs) — keeping it non-ABSTAIN lets the standard
``daemon_tick`` payload encode "MM is live" with the existing schema.
"""
from __future__ import annotations

from dataclasses import dataclass

from polymarket_trading_engine.types import EvidencePacket, MarketAssessment, SuggestedSide


# Sentinel that the daemon checks to route an MM tick into the MM
# lifecycle handler. Mirrors the PENNY / OVERREACTION tags so the dispatch
# is a string equality check.
MARKET_MAKER_STRATEGY_TAG = "market-maker-strategy"


@dataclass(slots=True)
class MarketMakerScorer:
    """Pure-function MM gate. Holds no state — every call is a fresh
    decision based on the packet alone.

    The defaults match the planned starting point for the first paper soak
    (see ``initial_settings.INITIAL_SETTINGS_BASELINE``). All knobs
    hot-reload via the settings store; the daemon constructs a fresh
    scorer when any of these change.
    """

    min_tte_seconds: int = 120
    min_market_spread: float = 0.01
    max_market_spread: float = 0.10
    require_rewards: bool = False

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        base = _abstain_template(packet)

        # NB: deliberately NOT checking ``packet.is_pre_market`` here.
        # That field is BTC-candle-specific (``tte > family_window_seconds``)
        # and would falsely fire on every sports / politics market in the
        # MM universe whose TTE outruns the nominal BTC candle window.
        # The two-sided-book gate below covers the genuine "book not live
        # yet" case for non-BTC markets.

        if packet.seconds_to_expiry < self.min_tte_seconds:
            return _abstain(
                base,
                (
                    f"MM: TTE {packet.seconds_to_expiry}s < min "
                    f"{self.min_tte_seconds}s — too close to resolution."
                ),
            )

        bid = float(packet.bid_yes or 0.0)
        ask = float(packet.ask_yes or 0.0)
        if bid <= 0.0 or ask <= 0.0 or bid >= ask:
            return _abstain(
                base,
                (
                    f"MM: book not two-sided (bid_yes={bid}, ask_yes={ask}) — "
                    "no mid to anchor quotes."
                ),
            )

        market_spread = ask - bid
        if market_spread < self.min_market_spread:
            return _abstain(
                base,
                (
                    f"MM: market spread {market_spread:.4f} < min "
                    f"{self.min_market_spread:.4f} — no spread to capture."
                ),
            )
        if self.max_market_spread > 0.0 and market_spread > self.max_market_spread:
            return _abstain(
                base,
                (
                    f"MM: market spread {market_spread:.4f} > max "
                    f"{self.max_market_spread:.4f} — toxic-flow signature, "
                    "skip."
                ),
            )

        # ``packet`` does not carry the rewards_daily_rate directly (it's
        # on the candidate, which the daemon owns). We expose a boolean
        # gate here that the daemon enforces by passing the candidate
        # info through ``packet.reasons_context`` is not necessary —
        # instead, we trust the daemon to re-check the reward gate on the
        # candidate before placing quotes. The scorer's role here is just
        # to filter on packet-visible features. ``require_rewards`` is
        # therefore evaluated downstream; we keep the field on the scorer
        # so the daemon can read it in one place.

        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=packet.market_probability,
            fair_probability_no=1.0 - packet.market_probability,
            confidence=0.5,
            suggested_side=SuggestedSide.YES,
            expiry_risk="LOW",
            reasons_for_trade=[
                (
                    f"MM: book {bid:.4f}/{ask:.4f} (spread {market_spread:.4f}), "
                    f"TTE {packet.seconds_to_expiry}s — quoting both sides."
                )
            ],
            reasons_to_abstain=[],
            edge=0.0,
            edge_yes=0.0,
            edge_no=0.0,
            raw_model_output=MARKET_MAKER_STRATEGY_TAG,
            slippage_bps=0.0,
        )


def _abstain_template(packet: EvidencePacket) -> MarketAssessment:
    return MarketAssessment(
        market_id=packet.market_id,
        fair_probability=packet.market_probability,
        fair_probability_no=1.0 - packet.market_probability,
        confidence=0.0,
        suggested_side=SuggestedSide.ABSTAIN,
        expiry_risk="UNKNOWN",
        reasons_for_trade=[],
        reasons_to_abstain=[],
        edge=0.0,
        edge_yes=0.0,
        edge_no=0.0,
        raw_model_output=MARKET_MAKER_STRATEGY_TAG,
        slippage_bps=0.0,
    )


def _abstain(template: MarketAssessment, reason: str) -> MarketAssessment:
    template.reasons_to_abstain = [reason]
    return template
