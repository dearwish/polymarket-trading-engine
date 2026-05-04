from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

_SLUG_WINDOW_SECONDS: dict[str, int] = {
    "btc_5m": 5 * 60,
    "btc_15m": 15 * 60,
}
_SLUG_PREFIX: dict[str, str] = {
    "btc_5m": "btc-updown-5m",
    "btc_15m": "btc-updown-15m",
}
_SLUG_PREDICTED_FAMILIES = frozenset({"btc_5m", "btc_15m", "btc_1h"})
_ET = ZoneInfo("America/New_York")


def _format_1h_et_slug(window_start_utc: datetime) -> str:
    """Polymarket hourly market slug: bitcoin-up-or-down-{month}-{day}-{year}-{hr}{am/pm}-et."""
    et = window_start_utc.astimezone(_ET)
    month = et.strftime("%B").lower()
    hour = et.hour
    if hour == 0:
        hr, ampm = "12", "am"
    elif hour < 12:
        hr, ampm = str(hour), "am"
    elif hour == 12:
        hr, ampm = "12", "pm"
    else:
        hr, ampm = str(hour - 12), "pm"
    return f"bitcoin-up-or-down-{month}-{et.day}-{et.year}-{hr}{ampm}-et"

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OpenOrderParams, OrderArgs, OrderType, TradeParams
from py_clob_client.order_builder.constants import BUY, SELL

from polymarket_trading_engine.config import Settings
from polymarket_trading_engine.engine.market_maker.scanner import score_mm_market
from polymarket_trading_engine.types import (
    AuthStatus,
    ExecutionMode,
    ExecutionResult,
    ExecutionStyle,
    MarketCandidate,
    OrderBookSnapshot,
    OrderSide,
    TradeDecision,
)


class PolymarketConnector:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)

    def discover_markets(self, limit: int = 25) -> list[MarketCandidate]:
        family = self.settings.market_family
        if family in _SLUG_PREDICTED_FAMILIES:
            return self._discover_by_slug_prediction(family)
        request_limit = self._discovery_request_limit(limit)
        params = {
            "closed": "false",
            "limit": request_limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        response = self.client.get(f"{self.settings.polymarket_gamma_url}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        markets = [candidate for item in payload if (candidate := self._parse_market(item))]
        return self._sort_market_candidates(markets)

    def _discover_by_slug_prediction(self, family: str, lookahead: int = 3) -> list[MarketCandidate]:
        """Fetch upcoming rolling btc-updown events by directly predicting their slugs.

        Polymarket does not surface these ephemeral short-horizon markets in bulk
        /markets or /events listings, so we compute the next few window-start slugs
        (unix-seconds aligned for 5m/15m, ET human-date+hour for 1h) and fetch each
        directly via /events/slug/<slug>.
        """
        seen: set[str] = set()
        candidates: list[MarketCandidate] = []
        for i in range(lookahead):
            slug = self._predicted_slug(family, i)
            if slug is None:
                continue
            candidate = self._fetch_event_slug_market(slug)
            if candidate is None or candidate.market_id in seen:
                continue
            seen.add(candidate.market_id)
            candidates.append(candidate)
        return self._sort_market_candidates(candidates)

    @staticmethod
    def _predicted_slug(family: str, window_index: int) -> str | None:
        if family in _SLUG_WINDOW_SECONDS:
            step = _SLUG_WINDOW_SECONDS[family]
            prefix = _SLUG_PREFIX[family]
            now_ts = int(time.time())
            window_start = (now_ts // step) * step + window_index * step
            return f"{prefix}-{window_start}"
        if family == "btc_1h":
            now_utc = datetime.now(timezone.utc)
            current_et_hour = now_utc.astimezone(_ET).replace(minute=0, second=0, microsecond=0)
            return _format_1h_et_slug(current_et_hour + timedelta(hours=window_index))
        return None

    def _fetch_event_slug_market(self, slug: str) -> MarketCandidate | None:
        url = f"{self.settings.polymarket_gamma_url}/events/slug/{slug}"
        try:
            response = self.client.get(url, timeout=5.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        markets = data.get("markets") or []
        if not markets:
            return None
        return self._parse_market(markets[0])

    def discover_active_market(self, limit: int = 50) -> MarketCandidate | None:
        markets = self.discover_markets(limit=limit)
        max_expiry_seconds = self._active_market_max_expiry_seconds()
        if max_expiry_seconds is not None:
            markets = [
                market
                for market in markets
                if 0 < self.estimate_seconds_to_expiry(market.end_date_iso) <= max_expiry_seconds
            ]
        return markets[0] if markets else None

    def get_market(self, market_id: str) -> MarketCandidate:
        """Fetch one specific market by id, bypassing the family filter.

        Direct lookups (orphan-close reconciliation, MM positions on
        non-BTC markets) need this to succeed even when the requested
        market doesn't match the operator-configured ``market_family``.
        Without ``apply_family_filter=False`` the parser silently
        returned ``None`` for any non-BTC market and the orphan-close
        path swallowed the resulting ``ValueError`` — which left
        resolved-but-still-OPEN MM positions in the DB indefinitely
        (observed 2026-05-03 on the MLB SF/TB position that resolved
        2 days earlier but never got force-closed).
        """
        response = self.client.get(f"{self.settings.polymarket_gamma_url}/markets/{market_id}")
        response.raise_for_status()
        candidate = self._parse_market(response.json(), apply_family_filter=False)
        if not candidate:
            raise ValueError(f"Unable to parse market {market_id}")
        return candidate

    def get_orderbook_snapshot(self, token_id: str) -> OrderBookSnapshot:
        response = self.client.get(f"{self.settings.polymarket_host}/book", params={"token_id": token_id})
        response.raise_for_status()
        data = response.json()
        bids = sorted(data.get("bids", []), key=lambda level: float(level["price"]), reverse=True)
        asks = sorted(data.get("asks", []), key=lambda level: float(level["price"]))
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        midpoint = round((best_bid + best_ask) / 2, 6) if best_bid and best_ask else best_bid or best_ask
        spread = round(max(best_ask - best_bid, 0.0), 6) if best_bid and best_ask else 0.0
        bid_depth = sum(float(level["price"]) * float(level["size"]) for level in bids[:5])
        ask_depth = sum(float(level["price"]) * float(level["size"]) for level in asks[:5])
        last_trade_price = float(data.get("last_trade_price") or data.get("lastTradePrice") or midpoint or 0.0)
        bid_levels = [(float(level["price"]), float(level["size"])) for level in bids[:10]]
        ask_levels = [(float(level["price"]), float(level["size"])) for level in asks[:10]]
        return OrderBookSnapshot(
            bid=best_bid,
            ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            depth_usd=bid_depth + ask_depth,
            last_trade_price=last_trade_price,
            two_sided=bool(best_bid and best_ask),
            bid_levels=bid_levels,
            ask_levels=ask_levels,
        )

    def get_auth_status(self) -> AuthStatus:
        private_key = self.settings.polymarket_private_key.strip()
        funder = self.settings.polymarket_funder.strip()
        missing: list[str] = []
        if not private_key:
            missing.append("polymarket_private_key")
        if self.settings.polymarket_signature_type in {1, 2} and not funder:
            missing.append("polymarket_funder")
        return AuthStatus(
            private_key_configured=bool(private_key),
            funder_configured=bool(funder),
            signature_type=self.settings.polymarket_signature_type,
            live_client_constructible=not missing,
            missing=missing,
        )

    def probe_live_readiness(self) -> AuthStatus:
        status = self.get_auth_status()
        if not status.live_client_constructible:
            return status
        status.probe_attempted = True
        try:
            client = self.build_live_client()
            status.wallet_address = str(client.get_address() or "")
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            status.api_credentials_derived = True
            status.server_ok = bool(client.get_ok())
            status.readonly_ready = status.api_credentials_derived and status.server_ok
            if status.readonly_ready:
                self._collect_account_diagnostics(client, status)
        except Exception as exc:
            status.errors.append(str(exc))
        return status

    def build_live_client(self) -> ClobClient:
        status = self.get_auth_status()
        if not status.live_client_constructible:
            raise ValueError(f"Missing live auth settings: {', '.join(status.missing)}")
        funder = self.settings.polymarket_funder.strip() or None
        return ClobClient(
            self.settings.polymarket_host,
            key=self.settings.polymarket_private_key.strip(),
            chain_id=self.settings.polymarket_chain_id,
            signature_type=self.settings.polymarket_signature_type,
            funder=funder,
        )

    def execute_live_trade(
        self,
        decision: TradeDecision,
        orderbook: OrderBookSnapshot | None = None,
    ) -> ExecutionResult:
        if not self.settings.live_trading_enabled:
            return ExecutionResult(
                market_id=decision.market_id,
                success=False,
                mode=ExecutionMode.LIVE,
                order_id="live-disabled",
                status="LIVE_DISABLED",
                detail="Live trading flag is disabled.",
                fill_price=0.0,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )
        if not decision.asset_id:
            return ExecutionResult(
                market_id=decision.market_id,
                success=False,
                mode=ExecutionMode.LIVE,
                order_id="live-missing-asset",
                status="LIVE_INVALID",
                detail="Live trade decision is missing the Polymarket asset_id/token_id.",
                fill_price=0.0,
                order_side=decision.order_side,
                asset_id=decision.asset_id,
                execution_style=decision.execution_style,
            )
        client = self._build_authed_live_client()
        share_size = round(decision.size_usd / max(decision.limit_price, 1e-6), 6)
        clob_side = BUY if decision.order_side == OrderSide.BUY else SELL
        order = client.create_order(
            OrderArgs(
                token_id=decision.asset_id,
                price=decision.limit_price,
                size=share_size,
                side=clob_side,
            )
        )
        order_type = self._live_order_type_for_decision(decision)
        post_only = decision.post_only or self.settings.live_post_only
        order_response = client.post_order(
            order,
            orderType=order_type,
            post_only=post_only,
        )
        order_id = str(
            order_response.get("orderID")
            or order_response.get("orderId")
            or order_response.get("id")
            or ""
        )
        status = str(order_response.get("status") or "LIVE_SUBMITTED")
        return ExecutionResult(
            market_id=decision.market_id,
            success=True,
            mode=ExecutionMode.LIVE,
            order_id=order_id,
            status=status,
            detail=(
                f"Live {decision.order_side.value} {share_size:.6f} shares @ {decision.limit_price:.4f} "
                f"via {decision.execution_style.value}"
            ),
            fill_price=0.0,
            order_side=decision.order_side,
            asset_id=decision.asset_id,
            execution_style=decision.execution_style,
            remaining_size_shares=share_size,
        )

    def list_live_orders(self) -> list[dict[str, Any]]:
        client = self._build_authed_live_client()
        orders = client.get_orders(OpenOrderParams())
        return [self._normalize_live_order(order) for order in orders]

    def get_live_order(self, order_id: str) -> dict[str, Any]:
        client = self._build_authed_live_client()
        return self._normalize_live_order(client.get_order(order_id))

    def cancel_live_order(self, order_id: str) -> dict[str, Any]:
        client = self._build_authed_live_client()
        response = client.cancel_orders([order_id])
        return self._normalize_cancel_response(order_id, response)

    def replace_live_order(
        self,
        decision: TradeDecision,
        existing_order_id: str,
    ) -> dict[str, Any]:
        """Cancel an existing resting order and post a replacement.

        Used by the execution engine's cancel/replace loop when a maker quote
        drifts off the best level. Returns a dict summarising both legs so
        callers can reconcile the replacement without a second round trip.
        """
        cancel_result = self.cancel_live_order(existing_order_id)
        new_result = self.execute_live_trade(decision)
        return {
            "cancelled": cancel_result,
            "replacement": {
                "order_id": new_result.order_id,
                "status": new_result.status,
                "detail": new_result.detail,
                "success": new_result.success,
            },
        }

    def list_live_trades(self, market_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        client = self._build_authed_live_client()
        params = TradeParams(market=market_id) if market_id else TradeParams()
        trades = client.get_trades(params)
        return [self._normalize_live_trade(trade) for trade in trades[:limit]]

    def list_market_trades(self, market_id: str, limit: int = 20) -> list[dict[str, Any]]:
        response = self.client.get(
            f"{self.settings.polymarket_data_url}/trades",
            params={"market": market_id, "limit": limit},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [self._normalize_market_trade(trade) for trade in payload[:limit]]

    def get_live_trade(self, trade_id: str, market_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        trades = self.list_live_trades(market_id=market_id, limit=limit)
        for trade in trades:
            if trade["trade_id"] == trade_id:
                return trade
        raise ValueError(f"Unable to find trade {trade_id} in recent authenticated trade history.")

    def _build_authed_live_client(self) -> ClobClient:
        client = self.build_live_client()
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return client

    def _collect_account_diagnostics(self, client: ClobClient, status: AuthStatus) -> None:
        try:
            status.collateral_address = str(client.get_collateral_address() or "")
        except Exception as exc:
            status.errors.append(f"collateral_address: {exc}")
        try:
            balance_payload = client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.settings.polymarket_signature_type,
                )
            )
            balance, allowance = self._extract_balance_allowance(balance_payload)
            status.balance = balance
            status.allowance = allowance
        except Exception as exc:
            status.errors.append(f"balance_allowance: {exc}")
        try:
            orders = client.get_orders(OpenOrderParams())
            status.open_orders_count = len(orders)
            status.open_orders_markets = self._extract_open_order_markets(orders)
        except Exception as exc:
            status.errors.append(f"open_orders: {exc}")
        status.diagnostics_collected = True

    @staticmethod
    def _normalize_market_trade(trade: dict[str, Any]) -> dict[str, Any]:
        return {
            "trade_id": str(trade.get("id") or trade.get("transactionHash") or ""),
            "market_id": str(trade.get("conditionId") or trade.get("market") or ""),
            "asset_id": str(trade.get("asset") or trade.get("asset_id") or ""),
            "side": str(trade.get("side") or ""),
            "outcome": str(trade.get("outcome") or ""),
            "price": float(trade.get("price") or 0.0),
            "size": float(trade.get("size") or 0.0),
            "timestamp": int(trade.get("timestamp") or 0),
            "title": str(trade.get("title") or ""),
            "slug": str(trade.get("slug") or ""),
        }

    def estimate_seconds_to_expiry(self, end_date_iso: str) -> int:
        try:
            expiry = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return -1
        return int((expiry - datetime.now(timezone.utc)).total_seconds())

    def _parse_market(
        self, item: dict[str, Any], apply_family_filter: bool = True
    ) -> MarketCandidate | None:
        token_ids = self._parse_token_ids(item.get("clobTokenIds"))
        if len(token_ids) < 2:
            return None
        question = item.get("question") or ""
        if apply_family_filter and not self._matches_market_family(item):
            return None
        yes_price, no_price = self._parse_outcome_prices(item.get("outcomePrices"))
        # Falsy-zero bug fix (2026-05-03): for a RESOLVED market with
        # YES outcome=0 (the YES side lost), ``outcomePrices = ["0", "1"]``
        # → yes_price = 0.0. The previous ``if yes_price`` test treated
        # 0.0 as "missing" and fell back to 0.5, which made the orphan-
        # close path mark a $1000 YES position as a $204 spurious WIN
        # instead of the −$1000 loss. Use sum-check instead: if the
        # outcomes don't sum to ~1.0, the market hasn't been parsed and
        # we use 0.5 as a neutral default.
        if abs(yes_price + no_price - 1.0) < 0.01:
            implied = yes_price
        else:
            implied = 0.5
        rewards_rate, rewards_max_spread, rewards_min_size = self._parse_rewards(item)
        tick_size = self._parse_tick_size(item)
        return MarketCandidate(
            market_id=str(item.get("id", "")),
            question=question,
            condition_id=item.get("conditionId", "") or "",
            slug=item.get("slug", "") or "",
            end_date_iso=item.get("endDate", "") or "",
            yes_token_id=token_ids[0],
            no_token_id=token_ids[1],
            implied_probability=implied,
            liquidity_usd=float(item.get("liquidityNum") or item.get("liquidityClob") or 0.0),
            volume_24h_usd=float(item.get("volume24hr") or item.get("volume24hrClob") or 0.0),
            resolution_source=item.get("description") or "",
            rewards_daily_rate=rewards_rate,
            rewards_max_spread_pct=rewards_max_spread,
            rewards_min_size=rewards_min_size,
            tick_size=tick_size,
            closed=bool(item.get("closed", False)),
        )

    def discover_mm_markets(
        self,
        *,
        min_rewards_daily_usd: float = 1.0,
        min_liquidity_usd: float = 5000.0,
        min_tte_seconds: int = 3600,
        max_markets: int = 5,
        fetch_limit: int = 200,
        max_eligible_min_size_usd: float | None = None,
    ) -> list[MarketCandidate]:
        """Scan Polymarket for the best MM-suitable markets, ranked by yield.

        The market-maker strategy needs a fundamentally different universe
        from the BTC short-horizon scorers: reward-paying, liquid, slow-
        moving markets where passive yield + spread capture compensates
        for adverse selection. This bypasses the family-filter slug
        prediction path and pulls the bulk ``/markets`` endpoint.

        Filters applied client-side:

        - ``rewards_daily_rate >= min_rewards_daily_usd`` (must pay maker
          subsidies — the MM thesis assumes the daily yield is the bulk
          of the edge, not the spread).
        - ``liquidity_usd >= min_liquidity_usd`` (thin books invite toxic
          flow and the spread can't be earned consistently).
        - ``end_date_iso`` parseable AND time-to-expiry ≥ ``min_tte_seconds``
          (a market resolving in the next hour can't accumulate enough
          fills to capture the spread reliably).
        - ``rewards_min_size <= max_eligible_min_size_usd`` (when supplied):
          drop markets whose reward-eligibility minimum order size exceeds
          our per-leg quote notional. Polymarket only counts a quote toward
          the daily reward pool if it meets ``rewards_min_size``; posting
          smaller is legal but earns the bare spread, defeating the strategy
          thesis. ``None`` skips this filter so paper-mode soaks can still
          exercise the lifecycle handler against real markets that the
          configured size won't actually qualify on.

        Ranking score (descending): ``daily_rate × 1000 / max(liquidity, 1000)``.
        This is yield per $1k of competing liquidity — boosts markets
        where the daily pool isn't already crowded with makers, dampens
        markets where everyone is already MM-ing. The pure-math scoring
        helper :func:`score_mm_market` is exposed so the daemon's reload
        loop can re-rank without re-fetching from the API.

        Returns up to ``max_markets`` candidates. Empty list if the API
        is unavailable, no markets pay rewards, or every candidate fails
        the filters — caller must treat empty as "skip the MM pipeline
        this cycle", not "all markets disqualified" (those are the same
        from the daemon's perspective).
        """
        params = {
            "closed": "false",
            "active": "true",
            "limit": int(fetch_limit),
            "order": "liquidityNum",
            "ascending": "false",
        }
        try:
            response = self.client.get(
                f"{self.settings.polymarket_gamma_url}/markets", params=params
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        if not isinstance(payload, list):
            return []

        scored: list[tuple[float, MarketCandidate]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            candidate = self._parse_market(item, apply_family_filter=False)
            if candidate is None:
                continue
            if candidate.rewards_daily_rate < min_rewards_daily_usd:
                continue
            if candidate.liquidity_usd < min_liquidity_usd:
                continue
            tte = self._seconds_to_expiry(candidate.end_date_iso)
            if tte < min_tte_seconds:
                continue
            if (
                max_eligible_min_size_usd is not None
                and candidate.rewards_min_size > max_eligible_min_size_usd
            ):
                # Reward-pool eligibility minimum exceeds our per-leg
                # quote size; posting here is legal but earns no subsidy.
                continue
            score = score_mm_market(candidate)
            if score <= 0.0:
                continue
            scored.append((score, candidate))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [candidate for _score, candidate in scored[:max_markets]]

    @staticmethod
    def _parse_rewards(item: dict[str, Any]) -> tuple[float, float, float]:
        """Extract (daily_rate, max_spread_pct, min_size) from a Polymarket
        market item. Returns zeros when rewards aren't exposed.

        Live Gamma shape verified 2026-05-01:

        - ``clobRewards``: top-level list of ``{rewardsDailyRate, assetAddress,
          startDate, endDate, ...}`` entries. USDC entries are the ones we want.
        - ``rewardsMaxSpread``: top-level number (cents around mid, e.g. 2.5).
        - ``rewardsMinSize``: top-level number (e.g. 100).

        Older nested-``rewards.rates[]`` and flat ``rewardsDailyRate`` shapes
        are kept as fallbacks for resilience against shape drift, but the
        live API does NOT use them — every reward-paying market in the
        2026-05-01 sweep had ``clobRewards`` populated and no nested ``rewards``
        object at all.
        """
        daily_rate = 0.0
        max_spread = 0.0
        min_size = 0.0

        # Primary path: top-level ``clobRewards`` list.
        clob_rewards = item.get("clobRewards") if isinstance(item.get("clobRewards"), list) else []
        for entry in clob_rewards:
            if not isinstance(entry, dict):
                continue
            address = str(
                entry.get("assetAddress") or entry.get("asset_address") or ""
            ).lower()
            # USDC is the only collateral on Polygon Polymarket; if no
            # address matches we still take the first nonzero rate as a
            # last-resort fallback.
            if address and "2791bca1f2de4661ed88a30c99a7a9449aa84174" not in address:
                continue
            try:
                rate_value = float(
                    entry.get("rewardsDailyRate")
                    or entry.get("rewards_daily_rate")
                    or 0.0
                )
            except (TypeError, ValueError):
                continue
            if rate_value > 0.0:
                daily_rate = rate_value
                break

        # Fallback: legacy nested ``rewards.rates[]`` shape (not seen in
        # current Gamma traffic but kept for resilience).
        if daily_rate == 0.0:
            rewards = item.get("rewards") if isinstance(item.get("rewards"), dict) else None
            if rewards:
                for rate in (rewards.get("rates") or []):
                    if not isinstance(rate, dict):
                        continue
                    address = str(
                        rate.get("asset_address") or rate.get("assetAddress") or ""
                    ).lower()
                    if address and "2791bca1f2de4661ed88a30c99a7a9449aa84174" not in address:
                        continue
                    try:
                        daily_rate = float(
                            rate.get("rewards_daily_rate")
                            or rate.get("rewardsDailyRate")
                            or 0.0
                        )
                        if daily_rate > 0.0:
                            break
                    except (TypeError, ValueError):
                        continue
                if max_spread == 0.0:
                    try:
                        max_spread = float(
                            rewards.get("max_spread") or rewards.get("maxSpread") or 0.0
                        )
                    except (TypeError, ValueError):
                        max_spread = 0.0
                if min_size == 0.0:
                    try:
                        min_size = float(
                            rewards.get("min_size") or rewards.get("minSize") or 0.0
                        )
                    except (TypeError, ValueError):
                        min_size = 0.0

        # Fallback: top-level scalar fields (the live shape — current Gamma
        # exposes max_spread / min_size at the root, not inside clobRewards).
        if max_spread == 0.0:
            try:
                max_spread = float(item.get("rewardsMaxSpread") or 0.0)
            except (TypeError, ValueError):
                max_spread = 0.0
        if min_size == 0.0:
            try:
                min_size = float(item.get("rewardsMinSize") or 0.0)
            except (TypeError, ValueError):
                min_size = 0.0
        if daily_rate == 0.0:
            try:
                daily_rate = float(item.get("rewardsDailyRate") or 0.0)
            except (TypeError, ValueError):
                daily_rate = 0.0
        return daily_rate, max_spread, min_size

    @staticmethod
    def _parse_tick_size(item: dict[str, Any]) -> float:
        for key in ("minimum_tick_size", "minimumTickSize", "tickSize"):
            raw = item.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0.0:
                return value
        return 0.01  # Polymarket CLOB default tick.

    def _sort_market_candidates(self, markets: list[MarketCandidate]) -> list[MarketCandidate]:
        def sort_key(candidate: MarketCandidate) -> tuple[int, float, float]:
            seconds_to_expiry = PolymarketConnector._seconds_to_expiry(candidate.end_date_iso)
            effective_expiry = seconds_to_expiry if seconds_to_expiry >= 0 else 10**9
            family_score = self._market_family_score(
                candidate.question,
                candidate.resolution_source,
                candidate.slug,
            )
            return (-family_score, effective_expiry, -candidate.volume_24h_usd, -candidate.liquidity_usd)

        return sorted(markets, key=sort_key)

    @staticmethod
    def _seconds_to_expiry(end_date_iso: str) -> int:
        try:
            expiry = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return -1
        return int((expiry - datetime.now(timezone.utc)).total_seconds())

    def _matches_market_family(self, item: dict[str, Any]) -> bool:
        question = str(item.get("question") or "")
        description = str(item.get("description") or "")
        slug = str(item.get("slug") or "")
        if self.settings.market_family == "btc_1h":
            return self._market_family_score(question, description, slug) >= 5
        if self.settings.market_family == "btc_15m":
            return self._market_family_score(question, description, slug) >= 3
        if self.settings.market_family == "btc_5m":
            return self._market_family_score(question, description, slug) >= 3
        if self.settings.market_family == "btc_daily_threshold":
            return self._market_family_score(question, description, slug) >= 4
        return True

    def _market_family_score(self, question: str, description: str, slug: str) -> int:
        if self.settings.market_family == "btc_1h":
            return self._btc_1h_match_score(question, description, slug)
        if self.settings.market_family == "btc_15m":
            return self._btc_15m_match_score(question, description, slug)
        if self.settings.market_family == "btc_5m":
            return self._btc_5m_match_score(question, description, slug)
        if self.settings.market_family == "btc_daily_threshold":
            return self._btc_daily_threshold_match_score(question, description, slug)
        return 0

    def _active_market_max_expiry_seconds(self) -> int | None:
        if self.settings.market_family == "btc_1h":
            return 3 * 60 * 60
        if self.settings.market_family == "btc_15m":
            return 30 * 60
        if self.settings.market_family == "btc_5m":
            return 20 * 60
        if self.settings.market_family == "btc_daily_threshold":
            return 48 * 60 * 60
        return None

    def _discovery_request_limit(self, requested_limit: int) -> int:
        if self.settings.market_family == "btc_1h":
            return max(requested_limit, 800)
        if self.settings.market_family in {"btc_15m", "btc_5m", "btc_daily_threshold"}:
            return max(requested_limit, 200)
        return requested_limit

    def _live_order_type(self) -> OrderType:
        value = self.settings.live_order_type.strip().upper()
        try:
            return getattr(OrderType, value)
        except AttributeError as exc:
            raise ValueError(f"Unsupported live_order_type: {self.settings.live_order_type}") from exc

    def _live_order_type_for_decision(self, decision: TradeDecision) -> OrderType:
        if decision.execution_style == ExecutionStyle.GTC_MAKER:
            try:
                return getattr(OrderType, "GTC")
            except AttributeError:
                return self._live_order_type()
        return self._live_order_type()

    @staticmethod
    def _normalize_live_order(order: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(order, dict):
            return {"raw": order}
        price = PolymarketConnector._coerce_float(
            order.get("price") or order.get("limit_price") or order.get("avgPrice")
        )
        size = PolymarketConnector._coerce_float(
            order.get("size") or order.get("original_size") or order.get("quantity")
        )
        remaining = PolymarketConnector._coerce_float(
            order.get("size_matched")
            or order.get("matched_size")
            or order.get("remaining")
            or order.get("remaining_size")
        )
        return {
            "order_id": str(order.get("id") or order.get("orderID") or order.get("orderId") or ""),
            "market_id": str(order.get("market") or order.get("market_id") or order.get("condition_id") or ""),
            "asset_id": str(order.get("asset_id") or order.get("token_id") or ""),
            "status": str(order.get("status") or order.get("state") or ""),
            "side": str(order.get("side") or ""),
            "price": price,
            "size": size,
            "size_matched": remaining,
            "created_at": str(order.get("created_at") or order.get("createdAt") or ""),
            "raw": order,
        }

    @staticmethod
    def _normalize_cancel_response(order_id: str, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            canceled = response.get("canceled") or response.get("cancelled") or response.get("data")
            if isinstance(canceled, list):
                success = order_id in {str(item) for item in canceled}
            else:
                success = bool(response.get("success") or response.get("ok") or canceled)
            return {
                "order_id": order_id,
                "success": success,
                "response": response,
            }
        if isinstance(response, list):
            return {
                "order_id": order_id,
                "success": order_id in {str(item) for item in response},
                "response": response,
            }
        return {
            "order_id": order_id,
            "success": False,
            "response": response,
        }

    @staticmethod
    def _normalize_live_trade(trade: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(trade, dict):
            return {"raw": trade}
        return {
            "trade_id": str(trade.get("id") or trade.get("tradeID") or trade.get("tradeId") or ""),
            "order_id": str(trade.get("order_id") or trade.get("orderID") or trade.get("orderId") or ""),
            "market_id": str(trade.get("market") or trade.get("market_id") or trade.get("condition_id") or ""),
            "asset_id": str(trade.get("asset_id") or trade.get("token_id") or ""),
            "status": str(trade.get("status") or trade.get("state") or ""),
            "side": str(trade.get("side") or ""),
            "price": PolymarketConnector._coerce_float(trade.get("price") or trade.get("avgPrice")),
            "size": PolymarketConnector._coerce_float(trade.get("size") or trade.get("quantity")),
            "amount": PolymarketConnector._coerce_float(trade.get("amount") or trade.get("usdc_size")),
            "created_at": str(trade.get("created_at") or trade.get("createdAt") or ""),
            "raw": trade,
        }

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            if value in ("", None):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_balance_allowance(payload: Any) -> tuple[float | None, float | None]:
        if not isinstance(payload, dict):
            return None, None
        balance = PolymarketConnector._extract_first_float(
            payload,
            [
                ("balance",),
                ("balance", "balance"),
                ("balance", "available"),
                ("available",),
            ],
        )
        if balance is not None:
            balance = balance / 1_000_000
        allowance = PolymarketConnector._extract_first_float(
            payload,
            [
                ("allowance",),
                ("allowance", "allowance"),
                ("allowance", "available"),
            ],
        )
        return balance, allowance

    @staticmethod
    def _extract_first_float(payload: dict[str, Any], paths: list[tuple[str, ...]]) -> float | None:
        for path in paths:
            current: Any = payload
            for part in path:
                if not isinstance(current, dict) or part not in current:
                    current = None
                    break
                current = current[part]
            if current is None:
                continue
            try:
                return float(current)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_open_order_markets(orders: list[Any]) -> list[str]:
        markets: set[str] = set()
        for order in orders:
            if not isinstance(order, dict):
                continue
            market = (
                order.get("market")
                or order.get("market_id")
                or order.get("condition_id")
                or order.get("asset_id")
                or ""
            )
            if market:
                markets.add(str(market))
        return sorted(markets)[:10]

    @staticmethod
    def _btc_1h_match_score(question: str, description: str, slug: str) -> int:
        joined = " ".join([question, description, slug]).lower()
        has_btc = "bitcoin" in joined or "btc" in joined
        # Rolling hourly markets use this slug pattern; reject decoys (e.g. "April 18?" daily markets).
        if "bitcoin-up-or-down-" not in joined or "-et" not in joined:
            return 0
        has_1h_window = (
            "1 hour" in joined
            or "one hour" in joined
            or "60 minutes" in joined
            or "hourly" in joined
            or re.search(r"\b1h\b", joined) is not None
        )
        has_direction = any(
            phrase in joined
            for phrase in (
                "up or down",
                "above or below",
                "higher or lower",
                "rise or fall",
                "go up or down",
            )
        )
        has_hourly_family_pattern = (
            "bitcoin up or down -" in joined
            or "bitcoin up or down" in joined
            or "btc-up-or-down" in joined
            or "bitcoin-up-or-down" in joined
        )
        if not has_btc:
            return 0
        score = 0
        if has_btc:
            score += 1
        if has_1h_window:
            score += 2
        if has_direction:
            score += 2
        if has_hourly_family_pattern:
            score += 1
        return score

    @staticmethod
    def _btc_15m_match_score(question: str, description: str, slug: str) -> int:
        joined = " ".join([question, description, slug]).lower()
        has_btc = "bitcoin" in joined or "btc" in joined
        if not has_btc:
            return 0
        # Rolling 15m markets carry this slug prefix; reject anything else to avoid
        # pulling daily "Up or Down" decoys into the 15m family.
        if "btc-updown-15m" not in joined:
            return 0
        has_15m_window = (
            "15 minutes" in joined
            or "fifteen minutes" in joined
            or "quarter hour" in joined
            or re.search(r"\b15m\b", joined) is not None
            or re.search(r"\b15-?minute\b", joined) is not None
        )
        has_direction = any(
            phrase in joined
            for phrase in (
                "up or down",
                "above or below",
                "higher or lower",
                "rise or fall",
                "go up or down",
            )
        )
        mentions_short_expiry = "minute" in joined or re.search(r"\b\d+m\b", joined) is not None
        score = 1  # BTC match
        if has_15m_window:
            score += 2
        elif mentions_short_expiry:
            score += 1
        if has_direction:
            score += 2
        return score

    @staticmethod
    def _btc_5m_match_score(question: str, description: str, slug: str) -> int:
        joined = " ".join([question, description, slug]).lower()
        has_btc = "bitcoin" in joined or "btc" in joined
        # Require the rolling 5m slug prefix so daily "Up or Down" markets can't sneak in.
        if "btc-updown-5m" not in joined:
            return 0
        has_5m_window = "5 minutes" in joined or "five minutes" in joined or re.search(r"\b5m\b", joined) is not None
        has_direction = any(
            phrase in joined
            for phrase in (
                "up or down",
                "above or below",
                "higher or lower",
                "rise or fall",
                "go up or down",
            )
        )
        mentions_short_expiry = "minute" in joined or re.search(r"\b\d+m\b", joined) is not None
        score = 0
        if has_btc:
            score += 1
        if has_5m_window:
            score += 2
        elif mentions_short_expiry:
            score += 1
        if has_direction:
            score += 2
        return score

    @staticmethod
    def _btc_daily_threshold_match_score(question: str, description: str, slug: str) -> int:
        joined = " ".join([question, description, slug]).lower()
        has_btc = "bitcoin" in joined or "btc" in joined
        if not has_btc:
            return 0
        has_threshold = any(
            phrase in joined
            for phrase in (
                "above $",
                "below $",
                "reach $",
                "dip to $",
                "price of bitcoin be above",
                "price of bitcoin be below",
            )
        )
        has_daily_timeframe = any(
            phrase in joined
            for phrase in (
                "on april",
                "on may",
                "on june",
                "today",
                "tomorrow",
                "this week",
            )
        ) or re.search(r"\bon [a-z]+ \d{1,2}\b", joined) is not None
        has_monthly_timeframe = any(
            phrase in joined
            for phrase in (
                "in april",
                "in may",
                "in june",
                "this month",
                "during the month",
                "by the end of the month",
            )
        )
        score = 0
        if has_btc:
            score += 1
        if has_threshold:
            score += 2
        if has_daily_timeframe:
            score += 2
        if has_monthly_timeframe:
            score -= 1
        return score

    @staticmethod
    def _parse_token_ids(raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            return [str(value) for value in raw_value]
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                stripped = stripped[1:-1]
            return [part.strip().strip('"') for part in stripped.split(",") if part.strip()]
        return []

    @staticmethod
    def _parse_outcome_prices(raw_value: Any) -> tuple[float, float]:
        if isinstance(raw_value, list) and len(raw_value) >= 2:
            return float(raw_value[0]), float(raw_value[1])
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                stripped = stripped[1:-1]
            parts = [part.strip().strip('"') for part in stripped.split(",") if part.strip()]
            if len(parts) >= 2:
                return float(parts[0]), float(parts[1])
        return 0.0, 0.0
