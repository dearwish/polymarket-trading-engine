from __future__ import annotations

from dataclasses import dataclass

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.types import (
    DecisionStatus,
    ExecutionStyle,
    OrderBookSnapshot,
    OrderSide,
    SuggestedSide,
    TradeDecision,
)


@dataclass(slots=True)
class RoutingDecision:
    style: ExecutionStyle
    post_only: bool
    limit_price: float
    reason: str


class ExecutionRouter:
    """Maker-first / taker-fallback router.

    Chooses ``GTC_MAKER`` with ``post_only=True`` when the edge is large enough
    to justify waiting for a passive fill and the market is not about to
    expire. Otherwise falls back to ``FOK_TAKER`` crossing the best opposite
    level. The chosen limit price is attached to the decision so downstream
    execution uses the router-aligned price instead of whatever the risk
    engine picked.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def route(
        self,
        decision: TradeDecision,
        orderbook: OrderBookSnapshot | None,
        seconds_to_expiry: int,
        edge: float,
    ) -> RoutingDecision:
        if decision.status != DecisionStatus.APPROVED:
            return RoutingDecision(
                style=ExecutionStyle.FOK_TAKER,
                post_only=False,
                limit_price=decision.limit_price,
                reason="decision_not_approved",
            )
        taker_price = self._taker_price(decision, orderbook)
        if not self._maker_eligible(edge, seconds_to_expiry, orderbook):
            return RoutingDecision(
                style=ExecutionStyle.FOK_TAKER,
                post_only=False,
                limit_price=taker_price,
                reason=self._maker_skip_reason(edge, seconds_to_expiry, orderbook),
            )
        maker_price = self._maker_price(decision, orderbook)
        return RoutingDecision(
            style=ExecutionStyle.GTC_MAKER,
            post_only=True,
            limit_price=maker_price,
            reason="maker_eligible",
        )

    def should_replace(
        self,
        existing_limit_price: float,
        orderbook: OrderBookSnapshot | None,
        decision: TradeDecision,
        existing_size: float | None = None,
        target_size: float | None = None,
    ) -> bool:
        """Return True when a resting maker quote should be cancel/replaced.

        Hysteresis-gated to avoid the cancel-thrash pattern (warproxxx /
        gamma-trade-lab): re-quote only if the fresh maker price has moved by
        more than ``execution_replace_min_ticks`` × ``execution_price_tick``,
        or — when both ``existing_size`` and ``target_size`` are supplied —
        the resting size deviates by more than
        ``execution_replace_min_size_pct``. The executor is responsible for
        computing the fresh price via :py:meth:`route` and re-submitting.
        """
        if orderbook is None:
            return False
        fresh_price = self._maker_price(decision, orderbook)
        tick = self.settings.execution_price_tick
        min_ticks = max(0.0, float(self.settings.execution_replace_min_ticks))
        price_threshold = tick * min_ticks
        if abs(fresh_price - existing_limit_price) > price_threshold:
            return True
        if existing_size is not None and target_size is not None and existing_size > 0:
            size_drift = abs(target_size - existing_size) / existing_size
            if size_drift > float(self.settings.execution_replace_min_size_pct):
                return True
        return False

    # --- internals -----------------------------------------------------

    def _maker_eligible(
        self,
        edge: float,
        seconds_to_expiry: int,
        orderbook: OrderBookSnapshot | None,
    ) -> bool:
        if orderbook is None or not orderbook.two_sided:
            return False
        if seconds_to_expiry <= self.settings.execution_maker_min_tte_seconds:
            return False
        if edge < self.settings.execution_maker_min_edge:
            return False
        return True

    def _maker_skip_reason(
        self,
        edge: float,
        seconds_to_expiry: int,
        orderbook: OrderBookSnapshot | None,
    ) -> str:
        if orderbook is None or not orderbook.two_sided:
            return "book_not_two_sided"
        if seconds_to_expiry <= self.settings.execution_maker_min_tte_seconds:
            return "tte_below_maker_floor"
        if edge < self.settings.execution_maker_min_edge:
            return "edge_below_maker_threshold"
        return "taker_fallback"

    def _taker_price(self, decision: TradeDecision, orderbook: OrderBookSnapshot | None) -> float:
        if orderbook is None:
            return decision.limit_price
        if decision.order_side == OrderSide.BUY:
            ask = orderbook.ask or decision.limit_price
            return round(min(0.999, ask), 6)
        bid = orderbook.bid or decision.limit_price
        return round(max(0.001, bid), 6)

    def _maker_price(self, decision: TradeDecision, orderbook: OrderBookSnapshot | None) -> float:
        if orderbook is None:
            return decision.limit_price
        tick = self.settings.execution_price_tick
        if decision.order_side == OrderSide.BUY:
            bid = orderbook.bid or 0.0
            if bid <= 0:
                return decision.limit_price
            # Join the bid; future revisions can bump by one tick.
            return round(max(0.001, bid), 6)
        ask = orderbook.ask or 0.0
        if ask <= 0:
            return decision.limit_price
        return round(min(0.999, ask), 6)

    def _side_label(self, side: SuggestedSide) -> str:
        return side.value
