"""Penny-buy scorer: extreme-tail dip-buying on Polymarket binary markets.

Thesis: when one side of a binary market trades at or below 3¢ with
enough TTE remaining, intra-market volatility gives a meaningful chance
of a bounce to 5–6¢ before resolution. The payoff is asymmetric — a
modest bounce pays 2× while a full loss caps at 1× — so a hit rate above
33% produces positive EV. Backtest on 8h of BTC-15m soak data (see
``scripts/backtest_penny.py``) showed:

  entry_thresh = 0.03, min_entry_tte = 300s  →  63.6% hit rate, +45.8% ROI

The scorer is deliberately minimal — it does not read BTC drift, HTF
returns, or regime labels. It reads only the candidate book and the
TTE, because the penny thesis is microstructure-driven: we only care
that one side is cheap enough AND we have enough time for the bounce
cycle.

Execution is coordinated by the daemon's ``_handle_penny_strategy``
branch. The scorer's ``raw_model_output`` is tagged so the daemon can
route penny entries around the fade/adaptive pipeline (which has
cooldowns, candle-elapsed gates, and a TP ladder that all assume a
mid-price market, not a 1–5¢ tail trade).
"""
from __future__ import annotations

from dataclasses import replace

from polymarket_trading_engine.types import EvidencePacket, MarketAssessment, SuggestedSide


# Sentinel that the daemon checks to route into the penny lifecycle. Kept
# as a module constant so the daemon's branch is a `==` comparison, not a
# string literal that can silently drift.
PENNY_STRATEGY_TAG = "penny-strategy"


class PennyScorer:
    """Tail dip-buyer. Produces an APPROVED assessment on the cheap side
    when the book exposes a sub-threshold ask AND enough TTE remains for
    a bounce to materialise.

    Thresholds default to the sweet spot identified by the 8h backtest;
    overriding them lets us sweep params without code changes. The
    scorer holds no state — every call is a pure function of the packet.
    """

    def __init__(
        self,
        entry_thresh: float = 0.03,
        min_entry_tte_seconds: int = 300,
        min_favorable_move_bps: float = 25.0,
    ):
        self.entry_thresh = entry_thresh
        self.min_entry_tte_seconds = min_entry_tte_seconds
        # Reversal-confirmation gate: the YES mid must have moved IN our
        # favour by at least this many bps over 30s before we enter.
        # Replaces the earlier "no strong adverse move" gate after the
        # 2026-04-24 soak showed that just waiting for the crash to PAUSE
        # wasn't enough — the pause was often temporary and the side kept
        # crashing. Requiring actual reversal (bid ticked back up,
        # reflected in YES-mid reversing toward our side) cuts out the
        # "knife pause" fake-outs too.
        #
        #   - NO buy wants YES mid DROPPING (NO bid rising)  → require
        #     recent_price_change_bps ≤ −threshold
        #   - YES buy wants YES mid RISING  → require
        #     recent_price_change_bps ≥ +threshold
        #
        # Set to 0 to disable the gate entirely.
        self.min_favorable_move_bps = min_favorable_move_bps

    def score_market(self, packet: EvidencePacket) -> MarketAssessment:
        """Return an APPROVED YES/NO assessment when a penny setup is live,
        or ABSTAIN otherwise. The returned assessment carries a flat
        ``edge`` so the normal risk engine's ``min_edge`` gate passes —
        the penny daemon branch is authoritative for actual entry logic.
        """
        base = _abstain_template(packet)
        # Pre-market: the candle hasn't opened so the book is stale and
        # TTE is meaningless. Abstain regardless of asks.
        if packet.is_pre_market:
            return replace(
                base,
                reasons_to_abstain=[
                    "Penny: pre-market — book not live yet.",
                    *base.reasons_to_abstain,
                ],
            )
        if packet.seconds_to_expiry < self.min_entry_tte_seconds:
            return replace(
                base,
                reasons_to_abstain=[
                    (
                        f"Penny: TTE {packet.seconds_to_expiry}s < min "
                        f"{self.min_entry_tte_seconds}s — no bounce window."
                    ),
                    *base.reasons_to_abstain,
                ],
            )

        ask_no = float(packet.ask_no or 0.0)
        ask_yes = float(packet.ask_yes or 0.0)
        side: SuggestedSide | None = None
        entry_price = 0.0
        if 0.0 < ask_no <= self.entry_thresh:
            side = SuggestedSide.NO
            entry_price = ask_no
        elif 0.0 < ask_yes <= self.entry_thresh:
            side = SuggestedSide.YES
            entry_price = ask_yes

        if side is None:
            return replace(
                base,
                reasons_to_abstain=[
                    (
                        f"Penny: no side at ≤ {self.entry_thresh} "
                        f"(ask_yes={ask_yes}, ask_no={ask_no})."
                    ),
                    *base.reasons_to_abstain,
                ],
            )

        # Reversal-confirmation gate: require YES mid to have moved IN
        # OUR FAVOUR by at least min_favorable_move_bps over the last 30s
        # BEFORE we enter. Strictly stronger than "no adverse move" — it
        # demands actual bounce evidence, not merely a pause in the
        # crash. Setting to 0 opts out entirely.
        recent_move_bps = float(packet.recent_price_change_bps or 0.0)
        if self.min_favorable_move_bps > 0.0:
            if side is SuggestedSide.NO and recent_move_bps > -self.min_favorable_move_bps:
                return replace(
                    base,
                    reasons_to_abstain=[
                        (
                            f"Penny: YES mid {recent_move_bps:+.0f}bps over 30s; "
                            f"need ≤ -{self.min_favorable_move_bps:.0f}bps (NO bounce "
                            f"evidence) before entering."
                        ),
                        *base.reasons_to_abstain,
                    ],
                )
            if side is SuggestedSide.YES and recent_move_bps < self.min_favorable_move_bps:
                return replace(
                    base,
                    reasons_to_abstain=[
                        (
                            f"Penny: YES mid {recent_move_bps:+.0f}bps over 30s; "
                            f"need ≥ +{self.min_favorable_move_bps:.0f}bps (YES bounce "
                            f"evidence) before entering."
                        ),
                        *base.reasons_to_abstain,
                    ],
                )

        # Flag the assessment with a high edge so the risk engine's
        # ``min_edge`` gate doesn't block — the penny branch uses its
        # own entry gates (size cap, cooldown off, no cross-position
        # interference). Edge is set to (1 - entry_price) which is the
        # mathematical ceiling if the market resolves in our favour;
        # realised edge depends on the TP target.
        edge = max(0.0, 1.0 - entry_price)
        fair_side = 1.0 if side is SuggestedSide.YES else 0.0
        return MarketAssessment(
            market_id=packet.market_id,
            fair_probability=fair_side,
            fair_probability_no=1.0 - fair_side,
            confidence=0.75,
            suggested_side=side,
            expiry_risk="LOW",
            reasons_for_trade=[
                (
                    f"Penny: {side.value} ask {entry_price:.4f} ≤ "
                    f"{self.entry_thresh} with TTE {packet.seconds_to_expiry}s "
                    f"(bounce thesis)."
                )
            ],
            reasons_to_abstain=[],
            edge=edge,
            edge_yes=edge if side is SuggestedSide.YES else 0.0,
            edge_no=edge if side is SuggestedSide.NO else 0.0,
            raw_model_output=PENNY_STRATEGY_TAG,
            slippage_bps=0.0,
        )


def _abstain_template(packet: EvidencePacket) -> MarketAssessment:
    """Shared ABSTAIN skeleton so the various early-exit paths return
    the same shape. Only reasons_to_abstain differs per case.
    """
    return MarketAssessment(
        market_id=packet.market_id,
        fair_probability=0.5,
        fair_probability_no=0.5,
        confidence=0.0,
        suggested_side=SuggestedSide.ABSTAIN,
        expiry_risk="UNKNOWN",
        reasons_for_trade=[],
        reasons_to_abstain=[],
        edge=0.0,
        edge_yes=0.0,
        edge_no=0.0,
        raw_model_output=PENNY_STRATEGY_TAG,
        slippage_bps=0.0,
    )
