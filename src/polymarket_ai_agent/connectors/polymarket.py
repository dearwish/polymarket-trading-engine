from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OpenOrderParams, OrderArgs, OrderType, TradeParams
from py_clob_client.order_builder.constants import BUY

from polymarket_ai_agent.config import Settings
from polymarket_ai_agent.types import AuthStatus, ExecutionMode, ExecutionResult, MarketCandidate, OrderBookSnapshot, TradeDecision


class PolymarketConnector:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)

    def discover_markets(self, limit: int = 25) -> list[MarketCandidate]:
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
        response = self.client.get(f"{self.settings.polymarket_gamma_url}/markets/{market_id}")
        response.raise_for_status()
        candidate = self._parse_market(response.json())
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
        return OrderBookSnapshot(
            bid=best_bid,
            ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            depth_usd=bid_depth + ask_depth,
            last_trade_price=last_trade_price,
            two_sided=bool(best_bid and best_ask),
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
            )
        client = self._build_authed_live_client()
        share_size = round(decision.size_usd / decision.limit_price, 6)
        order = client.create_order(
            OrderArgs(
                token_id=decision.asset_id,
                price=decision.limit_price,
                size=share_size,
                side=BUY,
            )
        )
        order_response = client.post_order(
            order,
            orderType=self._live_order_type(),
            post_only=self.settings.live_post_only,
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
            detail=f"Live order submitted for {share_size:.6f} shares at {decision.limit_price:.4f}",
            fill_price=0.0,
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

    def _parse_market(self, item: dict[str, Any]) -> MarketCandidate | None:
        token_ids = self._parse_token_ids(item.get("clobTokenIds"))
        if len(token_ids) < 2:
            return None
        question = item.get("question") or ""
        if not self._matches_market_family(item):
            return None
        yes_price, no_price = self._parse_outcome_prices(item.get("outcomePrices"))
        implied = yes_price if yes_price else 0.5
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
        )

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
        if self.settings.market_family == "btc_5m":
            return self._market_family_score(
                str(item.get("question") or ""),
                str(item.get("description") or ""),
                str(item.get("slug") or ""),
            ) >= 3
        if self.settings.market_family == "btc_daily_threshold":
            return self._market_family_score(
                str(item.get("question") or ""),
                str(item.get("description") or ""),
                str(item.get("slug") or ""),
            ) >= 4
        return True

    def _market_family_score(self, question: str, description: str, slug: str) -> int:
        if self.settings.market_family == "btc_5m":
            return self._btc_5m_match_score(question, description, slug)
        if self.settings.market_family == "btc_daily_threshold":
            return self._btc_daily_threshold_match_score(question, description, slug)
        return 0

    def _active_market_max_expiry_seconds(self) -> int | None:
        if self.settings.market_family == "btc_5m":
            return 20 * 60
        if self.settings.market_family == "btc_daily_threshold":
            return 48 * 60 * 60
        return None

    def _discovery_request_limit(self, requested_limit: int) -> int:
        if self.settings.market_family in {"btc_5m", "btc_daily_threshold"}:
            return max(requested_limit, 200)
        return requested_limit

    def _live_order_type(self) -> OrderType:
        value = self.settings.live_order_type.strip().upper()
        try:
            return getattr(OrderType, value)
        except AttributeError as exc:
            raise ValueError(f"Unsupported live_order_type: {self.settings.live_order_type}") from exc

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
    def _btc_5m_match_score(question: str, description: str, slug: str) -> int:
        joined = " ".join([question, description, slug]).lower()
        has_btc = "bitcoin" in joined or "btc" in joined
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
