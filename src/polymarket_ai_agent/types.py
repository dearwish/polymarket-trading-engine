from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SuggestedSide(str, Enum):
    YES = "YES"
    NO = "NO"
    ABSTAIN = "ABSTAIN"


class DecisionStatus(str, Enum):
    APPROVED = "APPROVED"
    ABSTAIN = "ABSTAIN"
    REJECTED = "REJECTED"


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(slots=True)
class MarketCandidate:
    market_id: str
    question: str
    condition_id: str
    slug: str
    end_date_iso: str
    yes_token_id: str
    no_token_id: str
    implied_probability: float
    liquidity_usd: float
    volume_24h_usd: float
    resolution_source: str = ""


@dataclass(slots=True)
class OrderBookSnapshot:
    bid: float
    ask: float
    midpoint: float
    spread: float
    depth_usd: float
    last_trade_price: float
    observed_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class MarketSnapshot:
    candidate: MarketCandidate
    orderbook: OrderBookSnapshot
    seconds_to_expiry: int
    recent_price_change_bps: float
    recent_trade_count: int
    external_price: float
    collected_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class EvidencePacket:
    market_id: str
    question: str
    resolution_criteria: str
    market_probability: float
    orderbook_midpoint: float
    spread: float
    depth_usd: float
    seconds_to_expiry: int
    external_price: float
    recent_price_change_bps: float
    recent_trade_count: int
    reasons_context: list[str]
    citations: list[str]
    generated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class MarketAssessment:
    market_id: str
    fair_probability: float
    confidence: float
    suggested_side: SuggestedSide
    expiry_risk: str
    reasons_for_trade: list[str]
    reasons_to_abstain: list[str]
    edge: float
    raw_model_output: str
    assessed_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AccountState:
    mode: ExecutionMode
    available_usd: float
    open_positions: int
    daily_realized_pnl: float
    rejected_orders: int = 0


@dataclass(slots=True)
class TradeDecision:
    market_id: str
    status: DecisionStatus
    side: SuggestedSide
    size_usd: float
    limit_price: float
    rationale: list[str]
    rejected_by: list[str]
    decided_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ExecutionResult:
    market_id: str
    success: bool
    mode: ExecutionMode
    order_id: str
    status: str
    detail: str
    executed_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class PositionRecord:
    market_id: str
    side: SuggestedSide
    size_usd: float
    entry_price: float
    opened_at: datetime = field(default_factory=utc_now)
    status: str = "OPEN"


@dataclass(slots=True)
class PositionAction:
    market_id: str
    action: str
    reason: str


@dataclass(slots=True)
class RiskState:
    approved: bool
    reasons: list[str]
    rejected_by: list[str]


@dataclass(slots=True)
class Report:
    session_id: str
    generated_at: datetime
    summary: str
    items: list[str]
