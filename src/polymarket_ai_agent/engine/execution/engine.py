from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.engine.execution.router import ExecutionRouter, RoutingDecision
from polymarket_ai_agent.types import (
    DecisionStatus,
    ExecutionMode,
    ExecutionResult,
    ExecutionStyle,
    OrderBookSnapshot,
    OrderSide,
    SuggestedSide,
    TradeDecision,
)


class ExecutionEngine:
    """Executes approved trade decisions in paper or live mode.

    * In paper mode, fills walk the live-book side opposite the order,
      aggregating price × size VWAP to produce a realistic fill price.
    * In live mode, the provided ``live_executor`` callable is invoked after
      the router selects between maker (GTC post-only) and taker (FOK) styles.
      The router-chosen style, limit price and post-only flag are baked onto
      the decision so the connector issues the right order type.
    """

    def __init__(
        self,
        mode: ExecutionMode,
        paper_entry_slippage_bps: float = 10.0,
        live_trading_enabled: bool = False,
        live_executor: Callable[[TradeDecision, OrderBookSnapshot | None], ExecutionResult] | None = None,
        router: ExecutionRouter | None = None,
        settings: Settings | None = None,
    ):
        self.mode = mode
        self._counter = 0
        self.paper_entry_slippage_bps = paper_entry_slippage_bps
        self.live_trading_enabled = live_trading_enabled
        self.live_executor = live_executor
        self.router = router
        self.settings = settings

    def execute_trade(
        self,
        decision: TradeDecision,
        orderbook: OrderBookSnapshot | None = None,
        seconds_to_expiry: int = 0,
        edge: float = 0.0,
    ) -> ExecutionResult:
        self._counter += 1
        order_id = f"{self.mode.value}-order-{self._counter:06d}"
        if decision.status != DecisionStatus.APPROVED:
            return ExecutionResult(
                market_id=decision.market_id,
                success=False,
                mode=self.mode,
                order_id=order_id,
                status="SKIPPED",
                detail="Trade decision not approved.",
                fill_price=0.0,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )

        routed = self._maybe_route(decision, orderbook, seconds_to_expiry, edge)
        decision = replace(
            decision,
            limit_price=routed.limit_price,
            execution_style=routed.style,
            post_only=routed.post_only,
        )

        if self.mode == ExecutionMode.PAPER:
            fill_price, filled_shares, remaining_shares = self._paper_entry_fill(decision, orderbook)
            return ExecutionResult(
                market_id=decision.market_id,
                success=True,
                mode=self.mode,
                order_id=order_id,
                status="FILLED_PAPER",
                detail=(
                    f"Paper {decision.order_side.value} {filled_shares:.6f}/{filled_shares + remaining_shares:.6f} "
                    f"shares of {decision.side.value} @ {fill_price:.4f} [{routed.style.value}]"
                ),
                fill_price=fill_price,
                filled_size_shares=filled_shares,
                remaining_size_shares=remaining_shares,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )
        if not self.live_trading_enabled:
            return ExecutionResult(
                market_id=decision.market_id,
                success=False,
                mode=self.mode,
                order_id=order_id,
                status="LIVE_DISABLED",
                detail="Live execution is disabled. Set LIVE_TRADING_ENABLED=true to allow real order posting.",
                fill_price=0.0,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )
        if not decision.asset_id:
            return ExecutionResult(
                market_id=decision.market_id,
                success=False,
                mode=self.mode,
                order_id=order_id,
                status="LIVE_INVALID",
                detail="Approved live decision is missing the Polymarket asset_id/token_id.",
                fill_price=0.0,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )
        if not self.live_executor:
            return ExecutionResult(
                market_id=decision.market_id,
                success=False,
                mode=self.mode,
                order_id=order_id,
                status="LIVE_NOT_CONFIGURED",
                detail="Live execution is enabled but no live executor has been configured.",
                fill_price=0.0,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )
        return self.live_executor(decision, orderbook)

    def manage_open_positions(self) -> list:
        return []

    # --- internals -----------------------------------------------------

    def _maybe_route(
        self,
        decision: TradeDecision,
        orderbook: OrderBookSnapshot | None,
        seconds_to_expiry: int,
        edge: float,
    ) -> RoutingDecision:
        if self.router is None:
            return RoutingDecision(
                style=decision.execution_style,
                post_only=decision.post_only,
                limit_price=decision.limit_price,
                reason="no_router",
            )
        return self.router.route(decision, orderbook, seconds_to_expiry, edge)

    def _paper_entry_fill(
        self,
        decision: TradeDecision,
        orderbook: OrderBookSnapshot | None,
    ) -> tuple[float, float, float]:
        """Walk the book to compute a VWAP fill for the paper-mode order.

        For BUYs we consume asks from best to worst; for SELLs we consume bids
        in reverse. If the book has no levels we fall back to a constant-bps
        slippage on the reference price so the existing tests keep passing.

        NO-side trades: the passed orderbook is the YES book, so we reflect it
        to the NO side using the Polymarket invariant NO_price = 1 - YES_price.
        Buying NO == consuming YES bids at (1 - bid) in ascending NO-price order.
        Selling NO == consuming YES asks at (1 - ask) in descending NO-price order.
        Without this the fill price is recorded in the wrong token frame and PnL
        comes out inverted.
        """
        levels = self._levels_for_side(decision.order_side, orderbook)
        if decision.side == SuggestedSide.NO:
            # Flip YES levels into the NO frame and re-sort.
            opposite = list(orderbook.bid_levels) if decision.order_side == OrderSide.BUY else list(orderbook.ask_levels)
            flipped = [(max(0.01, min(0.99, 1.0 - price)), size) for price, size in opposite if size > 0]
            flipped.sort(key=lambda lvl: lvl[0], reverse=(decision.order_side == OrderSide.SELL))
            levels = flipped
        target_shares = decision.size_usd / max(decision.limit_price, 1e-6)
        if not levels:
            fallback = self._constant_slippage_price(decision.limit_price, orderbook, decision.order_side, side=decision.side)
            return fallback, target_shares, 0.0

        slippage_multiplier = 1 + self.paper_entry_slippage_bps / 10_000.0
        if decision.order_side == OrderSide.SELL:
            slippage_multiplier = 1 - self.paper_entry_slippage_bps / 10_000.0

        remaining = target_shares
        notional = 0.0
        filled = 0.0
        for price, size in levels:
            if remaining <= 0.0:
                break
            take = min(remaining, max(size, 0.0))
            if take <= 0.0:
                continue
            effective_price = max(0.01, min(0.99, price * slippage_multiplier))
            notional += effective_price * take
            filled += take
            remaining -= take
        if filled <= 0.0:
            fallback = self._constant_slippage_price(decision.limit_price, orderbook, decision.order_side, side=decision.side)
            return fallback, 0.0, target_shares
        vwap = round(notional / filled, 6)
        return vwap, round(filled, 6), round(max(remaining, 0.0), 6)

    @staticmethod
    def _levels_for_side(
        order_side: OrderSide,
        orderbook: OrderBookSnapshot | None,
    ) -> list[tuple[float, float]]:
        if orderbook is None:
            return []
        return list(orderbook.ask_levels) if order_side == OrderSide.BUY else list(orderbook.bid_levels)

    def _constant_slippage_price(
        self,
        limit_price: float,
        orderbook: OrderBookSnapshot | None,
        order_side: OrderSide,
        side: SuggestedSide = SuggestedSide.YES,
    ) -> float:
        if orderbook is not None:
            yes_reference = orderbook.ask if order_side == OrderSide.BUY else orderbook.bid
            if yes_reference <= 0.0:
                yes_reference = limit_price
            # For NO trades, flip into the NO frame: buying NO uses (1 - YES_bid);
            # selling NO uses (1 - YES_ask). The yes_reference chosen above is the
            # YES ask (for BUY) or YES bid (for SELL), so we flip the OPPOSITE field.
            if side == SuggestedSide.NO:
                opposite = orderbook.bid if order_side == OrderSide.BUY else orderbook.ask
                if opposite <= 0.0:
                    opposite = 1.0 - limit_price
                reference = max(0.01, min(0.99, 1.0 - opposite))
            else:
                reference = yes_reference
        else:
            reference = limit_price
        delta = reference * (self.paper_entry_slippage_bps / 10_000.0)
        signed = reference + delta if order_side == OrderSide.BUY else reference - delta
        return round(max(0.01, min(0.99, signed)), 6)
