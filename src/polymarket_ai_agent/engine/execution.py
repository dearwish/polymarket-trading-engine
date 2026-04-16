from __future__ import annotations

from polymarket_ai_agent.types import DecisionStatus, ExecutionMode, ExecutionResult, TradeDecision


class ExecutionEngine:
    def __init__(self, mode: ExecutionMode):
        self.mode = mode
        self._counter = 0

    def execute_trade(self, decision: TradeDecision) -> ExecutionResult:
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
            )
        if self.mode == ExecutionMode.PAPER:
            return ExecutionResult(
                market_id=decision.market_id,
                success=True,
                mode=self.mode,
                order_id=order_id,
                status="FILLED_PAPER",
                detail=f"Paper trade executed for {decision.side.value} at {decision.limit_price:.4f}",
            )
        return ExecutionResult(
            market_id=decision.market_id,
            success=False,
            mode=self.mode,
            order_id=order_id,
            status="NOT_IMPLEMENTED",
            detail="Live execution path is intentionally disabled in this scaffold.",
        )

    def manage_open_positions(self) -> list:
        return []
