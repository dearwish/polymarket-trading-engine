from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class OrderBookSide:
    """Price -> size map for one side of a CLOB order book.

    Levels with size <= 0 are removed. Bids are sorted high-to-low, asks
    low-to-high when read via :py:meth:`sorted_levels`.
    """

    is_bid: bool
    levels: dict[float, float] = field(default_factory=dict)

    def replace_levels(self, levels: list[tuple[float, float]]) -> None:
        self.levels = {price: size for price, size in levels if size > 0.0}

    def apply_change(self, price: float, size: float) -> None:
        if size <= 0.0:
            self.levels.pop(price, None)
        else:
            self.levels[price] = size

    def sorted_levels(self) -> list[tuple[float, float]]:
        if self.is_bid:
            return sorted(self.levels.items(), key=lambda item: item[0], reverse=True)
        return sorted(self.levels.items(), key=lambda item: item[0])

    def top(self) -> tuple[float, float] | None:
        levels = self.sorted_levels()
        return levels[0] if levels else None

    def depth_usd(self, max_levels: int = 5) -> float:
        total = 0.0
        for price, size in self.sorted_levels()[:max_levels]:
            total += price * size
        return total


@dataclass(slots=True)
class TokenBook:
    asset_id: str
    bids: OrderBookSide = field(default_factory=lambda: OrderBookSide(is_bid=True))
    asks: OrderBookSide = field(default_factory=lambda: OrderBookSide(is_bid=False))
    last_trade_price: float = 0.0
    last_update: datetime = field(default_factory=_utc_now)

    def best_bid(self) -> float:
        top = self.bids.top()
        return top[0] if top else 0.0

    def best_ask(self) -> float:
        top = self.asks.top()
        return top[0] if top else 0.0

    def mid(self) -> float:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid and ask:
            return round((bid + ask) / 2, 6)
        return bid or ask

    def spread(self) -> float:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid and ask:
            return round(max(ask - bid, 0.0), 6)
        return 0.0

    def microprice(self) -> float:
        bid_top = self.bids.top()
        ask_top = self.asks.top()
        if not bid_top or not ask_top:
            return self.mid()
        bid_price, bid_size = bid_top
        ask_price, ask_size = ask_top
        total = bid_size + ask_size
        if total <= 0.0:
            return self.mid()
        return round((ask_price * bid_size + bid_price * ask_size) / total, 6)

    def imbalance_top5(self) -> float:
        bid_depth = self.bids.depth_usd(5)
        ask_depth = self.asks.depth_usd(5)
        total = bid_depth + ask_depth
        if total <= 0.0:
            return 0.0
        return round((bid_depth - ask_depth) / total, 6)

    def depth_usd(self, max_levels: int = 5) -> float:
        return self.bids.depth_usd(max_levels) + self.asks.depth_usd(max_levels)

    def two_sided(self) -> bool:
        return bool(self.best_bid() and self.best_ask())


@dataclass(slots=True)
class MarketFeatures:
    market_id: str
    yes_token_id: str
    no_token_id: str
    bid_yes: float
    ask_yes: float
    bid_no: float
    ask_no: float
    mid_yes: float
    mid_no: float
    microprice_yes: float
    spread_yes: float
    depth_usd_yes: float
    imbalance_top5_yes: float
    last_trade_price_yes: float
    signed_flow_5s: float
    trade_count_5s: int
    last_update_age_seconds: float
    two_sided: bool


class MarketState:
    """In-memory per-market state driven by Polymarket CLOB websocket events.

    Holds a :class:`TokenBook` for both the YES and NO tokens, plus a rolling
    trade tape used to compute short-horizon signed flow. All mutators are
    synchronous and cheap — intended to be called from the websocket consumer
    loop without any I/O.
    """

    def __init__(
        self,
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
        trade_tape_max: int = 256,
        signed_flow_window_seconds: float = 5.0,
    ):
        self.market_id = market_id
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.yes_book = TokenBook(asset_id=yes_token_id)
        self.no_book = TokenBook(asset_id=no_token_id)
        self.trade_tape: deque[tuple[datetime, str, float, float, str]] = deque(maxlen=trade_tape_max)
        self._signed_flow_window_seconds = max(1.0, signed_flow_window_seconds)
        self.last_update: datetime = _utc_now()

    def apply_book_snapshot(self, payload: dict[str, Any]) -> None:
        asset_id = str(payload.get("asset_id") or "")
        book = self._book_for(asset_id)
        if book is None:
            return
        bids = [(_coerce_float(level.get("price")), _coerce_float(level.get("size"))) for level in payload.get("bids", [])]
        asks = [(_coerce_float(level.get("price")), _coerce_float(level.get("size"))) for level in payload.get("asks", [])]
        book.bids.replace_levels(bids)
        book.asks.replace_levels(asks)
        book.last_update = _utc_now()
        self.last_update = book.last_update

    def apply_price_change(self, payload: dict[str, Any]) -> None:
        asset_id = str(payload.get("asset_id") or "")
        book = self._book_for(asset_id)
        if book is None:
            return
        changes = payload.get("price_changes") or payload.get("changes") or []
        for change in changes:
            price = _coerce_float(change.get("price"))
            size = _coerce_float(change.get("size"))
            side = str(change.get("side") or "").upper()
            if price <= 0.0:
                continue
            if side == "BUY":
                book.bids.apply_change(price, size)
            elif side == "SELL":
                book.asks.apply_change(price, size)
            else:
                # If side is not explicit, default to closer side by mid.
                mid = book.mid()
                target = book.bids if price <= mid else book.asks
                target.apply_change(price, size)
        book.last_update = _utc_now()
        self.last_update = book.last_update

    def apply_last_trade(self, payload: dict[str, Any]) -> None:
        asset_id = str(payload.get("asset_id") or "")
        book = self._book_for(asset_id)
        if book is None:
            return
        price = _coerce_float(payload.get("price"))
        size = _coerce_float(payload.get("size"))
        side = str(payload.get("side") or "").upper()
        timestamp = _utc_now()
        if price > 0.0:
            book.last_trade_price = price
            book.last_update = timestamp
            self.last_update = timestamp
        if price > 0.0 and size > 0.0:
            self.trade_tape.append((timestamp, asset_id, price, size, side))

    def _book_for(self, asset_id: str) -> TokenBook | None:
        if not asset_id:
            return None
        if asset_id == self.yes_token_id:
            return self.yes_book
        if asset_id == self.no_token_id:
            return self.no_book
        return None

    def signed_flow(self, window_seconds: float | None = None, now: datetime | None = None) -> tuple[float, int]:
        window = window_seconds if window_seconds is not None else self._signed_flow_window_seconds
        current = now or _utc_now()
        cutoff = current.timestamp() - window
        flow = 0.0
        count = 0
        for ts, asset_id, price, size, side in reversed(self.trade_tape):
            if ts.timestamp() < cutoff:
                break
            count += 1
            sign = self._trade_sign(asset_id, side)
            flow += sign * price * size
        return flow, count

    def _trade_sign(self, asset_id: str, side: str) -> float:
        # Positive flow = YES-demand pressure on the up direction.
        if asset_id == self.yes_token_id:
            if side == "BUY":
                return 1.0
            if side == "SELL":
                return -1.0
        if asset_id == self.no_token_id:
            if side == "BUY":
                return -1.0
            if side == "SELL":
                return 1.0
        return 0.0

    def features(self, now: datetime | None = None) -> MarketFeatures:
        current = now or _utc_now()
        flow, trade_count = self.signed_flow(now=current)
        age = max(0.0, (current - self.last_update).total_seconds())
        return MarketFeatures(
            market_id=self.market_id,
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
            bid_yes=self.yes_book.best_bid(),
            ask_yes=self.yes_book.best_ask(),
            bid_no=self.no_book.best_bid(),
            ask_no=self.no_book.best_ask(),
            mid_yes=self.yes_book.mid(),
            mid_no=self.no_book.mid(),
            microprice_yes=self.yes_book.microprice(),
            spread_yes=self.yes_book.spread(),
            depth_usd_yes=self.yes_book.depth_usd(5),
            imbalance_top5_yes=self.yes_book.imbalance_top5(),
            last_trade_price_yes=self.yes_book.last_trade_price,
            signed_flow_5s=flow,
            trade_count_5s=trade_count,
            last_update_age_seconds=age,
            two_sided=self.yes_book.two_sided(),
        )
