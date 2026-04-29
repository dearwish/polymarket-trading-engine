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


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStyle(str, Enum):
    FOK_TAKER = "FOK_TAKER"
    GTC_MAKER = "GTC_MAKER"


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
    # Maker-reward parameters from Polymarket's CLOB ``rewards`` object.
    # When the market has no maker incentives these stay at 0 and the
    # reward-yield estimator returns 0 — markets without rewards are a
    # valid state, not an error. See ``engine/maker_rewards.py``.
    rewards_daily_rate: float = 0.0
    rewards_max_spread_pct: float = 0.0
    rewards_min_size: float = 0.0
    tick_size: float = 0.01


@dataclass(slots=True)
class OrderBookSnapshot:
    bid: float
    ask: float
    midpoint: float
    spread: float
    depth_usd: float
    last_trade_price: float
    two_sided: bool = True
    bid_levels: list[tuple[float, float]] = field(default_factory=list)
    ask_levels: list[tuple[float, float]] = field(default_factory=list)
    observed_at: datetime = field(default_factory=utc_now)
    # NO-side top-of-book quotes. Carried alongside the YES-side ``bid``/
    # ``ask`` so strategy-agnostic gates (RiskEngine entry-price floor /
    # ceiling) can resolve the chosen-side ask without re-deriving it from
    # parity. Defaults to 0.0 for legacy callers; the daemon's snapshot
    # builder populates these from per-tick features.
    bid_no: float = 0.0
    ask_no: float = 0.0


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
    bid_yes: float = 0.0
    ask_yes: float = 0.0
    bid_no: float = 0.0
    ask_no: float = 0.0
    microprice_yes: float = 0.0
    imbalance_top5_yes: float = 0.0
    signed_flow_5s: float = 0.0
    btc_log_return_30s: float = 0.0
    btc_log_return_5m: float = 0.0
    btc_log_return_15m: float = 0.0
    realized_vol_30m: float = 0.0
    # For threshold markets ("above $K"): ln(BTC_now / K). Zero for non-threshold.
    btc_log_return_vs_strike: float = 0.0
    # For directional "Up or Down" candle markets: ln(BTC_now / BTC_at_candle_open).
    # This is the drift observed SO FAR inside the market's own window, which is
    # what the GBM model needs: P(YES) = Φ(Δ_observed / (σ√τ_remaining)). Zero when
    # we haven't observed enough BTC history or the window hasn't started.
    btc_log_return_since_candle_open: float = 0.0
    time_elapsed_in_candle_s: int = 0
    # Pre-market: candle family market whose candle hasn't opened yet
    # (``seconds_to_expiry`` exceeds the family window length). The scorer
    # must not use rolling 5m/15m returns here — they're not predictive of
    # this candle's close-vs-open direction, and the edges they produce have
    # historically been the worst-performing bucket on the dashboard.
    is_pre_market: bool = False
    # Coarse UTC session tag (asia/eu/us/off) derived from the BTC snapshot's
    # observed_at. Instrumentation-only today — logged in daemon_tick so we can
    # stratify hit-rate / Brier by session before wiring it into the scorer.
    btc_session: str = "off"
    # Higher-timeframe log returns, derived from the BTC 1-minute bar buffer
    # (backfilled from Binance /klines on startup). Instrumentation-only:
    # logged in daemon_tick so we can study predictive value before any
    # scorer change.
    btc_log_return_1h: float = 0.0
    btc_log_return_4h: float = 0.0
    btc_log_return_24h: float = 0.0
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
    edge_yes: float = 0.0
    edge_no: float = 0.0
    fair_probability_no: float = 0.0
    slippage_bps: float = 0.0
    assessed_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AccountState:
    mode: ExecutionMode
    available_usd: float
    open_positions: int
    daily_realized_pnl: float
    rejected_orders: int = 0
    long_btc_exposure_usd: float = 0.0
    short_btc_exposure_usd: float = 0.0
    net_btc_exposure_usd: float = 0.0
    total_exposure_usd: float = 0.0


@dataclass(slots=True)
class TradeDecision:
    market_id: str
    status: DecisionStatus
    side: SuggestedSide
    size_usd: float
    limit_price: float
    rationale: list[str]
    rejected_by: list[str]
    asset_id: str = ""
    order_side: OrderSide = OrderSide.BUY
    intent: str = "OPEN"
    execution_style: ExecutionStyle = ExecutionStyle.FOK_TAKER
    post_only: bool = False
    decided_at: datetime = field(default_factory=utc_now)
    strategy_id: str = "fade"


@dataclass(slots=True)
class ExecutionResult:
    market_id: str
    success: bool
    mode: ExecutionMode
    order_id: str
    status: str
    detail: str
    fill_price: float = 0.0
    filled_size_shares: float = 0.0
    remaining_size_shares: float = 0.0
    execution_style: ExecutionStyle = ExecutionStyle.FOK_TAKER
    order_side: OrderSide = OrderSide.BUY
    asset_id: str = ""
    executed_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class PositionRecord:
    market_id: str
    side: SuggestedSide
    size_usd: float
    entry_price: float
    order_id: str = ""
    opened_at: datetime = field(default_factory=utc_now)
    status: str = "OPEN"
    close_reason: str = ""
    closed_at: datetime | None = None
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    strategy_id: str = "fade"


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
class AuthStatus:
    private_key_configured: bool
    funder_configured: bool
    signature_type: int
    live_client_constructible: bool
    missing: list[str]
    wallet_address: str = ""
    api_credentials_derived: bool = False
    server_ok: bool = False
    readonly_ready: bool = False
    probe_attempted: bool = False
    collateral_address: str = ""
    balance: float | None = None
    allowance: float | None = None
    open_orders_count: int = 0
    open_orders_markets: list[str] = field(default_factory=list)
    diagnostics_collected: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Report:
    session_id: str
    generated_at: datetime
    summary: str
    items: list[str]
