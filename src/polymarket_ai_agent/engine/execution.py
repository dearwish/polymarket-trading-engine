from __future__ import annotations

from polymarket_ai_agent.types import DecisionStatus, ExecutionMode, ExecutionResult, OrderBookSnapshot, TradeDecision


class ExecutionEngine:
    def __init__(self, mode: ExecutionMode, paper_entry_slippage_bps: float = 10.0):
        self.mode = mode
        self._counter = 0
        self.paper_entry_slippage_bps = paper_entry_slippage_bps

    def execute_trade(self, decision: TradeDecision, orderbook: OrderBookSnapshot | None = None) -> ExecutionResult:
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
            )
        if self.mode == ExecutionMode.PAPER:
            fill_price = self._paper_entry_fill_price(decision.limit_price, orderbook)
            return ExecutionResult(
                market_id=decision.market_id,
                success=True,
                mode=self.mode,
                order_id=order_id,
                status="FILLED_PAPER",
                detail=f"Paper trade executed for {decision.side.value} at {fill_price:.4f}",
                fill_price=fill_price,
            )
        return ExecutionResult(
            market_id=decision.market_id,
            success=False,
            mode=self.mode,
            order_id=order_id,
            status="NOT_IMPLEMENTED",
            detail="Live execution path is intentionally disabled in this scaffold.",
            fill_price=0.0,
        )

    def manage_open_positions(self) -> list:
        return []

    def _paper_entry_fill_price(self, limit_price: float, orderbook: OrderBookSnapshot | None) -> float:
        reference = orderbook.ask if orderbook else limit_price
        slippage = reference * (self.paper_entry_slippage_bps / 10_000)
        return round(min(0.99, max(0.01, reference + slippage)), 6)
