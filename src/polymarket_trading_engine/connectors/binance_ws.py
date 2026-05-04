from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass(slots=True)
class BtcTick:
    price: float
    observed_at: datetime
    source: str  # "aggTrade" | "bookTicker" | "rest"
    # aggTrade payloads carry the executed quantity (base-asset units). Other
    # sources leave this at 0.0 — they don't correspond to a trade event.
    quantity: float = 0.0


def _utc_from_ms(ms: int | None) -> datetime:
    if not ms:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


class BinanceBtcFeed:
    """BTC/USDT price feed with websocket primary + REST fallback.

    The websocket path uses Binance's combined-stream endpoint so a single
    connection delivers both ``aggTrade`` (executions) and ``bookTicker``
    (best bid/ask) updates for the configured symbol. If the websocket cannot
    be reached, callers can fall back to :py:meth:`rest_price` for a single
    synchronous REST pull.
    """

    def __init__(
        self,
        ws_url: str = "wss://stream.binance.com:9443/stream",
        rest_url: str = "https://api.binance.com/api/v3/ticker/price",
        symbol: str = "btcusdt",
        reconnect_backoff_seconds: float = 2.0,
        reconnect_backoff_max_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
        ssl_verify: bool = True,
    ):
        self._ws_url = ws_url
        self._rest_url = rest_url
        self._symbol = symbol.lower()
        self._reconnect_backoff_seconds = max(0.1, reconnect_backoff_seconds)
        self._reconnect_backoff_max_seconds = max(
            self._reconnect_backoff_seconds,
            reconnect_backoff_max_seconds,
        )
        self._http_client = http_client or httpx.Client(timeout=10, verify=ssl_verify)
        self._ssl_context: ssl.SSLContext | bool = ssl_verify or self._make_insecure_ctx()

    @staticmethod
    def _make_insecure_ctx() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def stream_url(self) -> str:
        streams = f"{self._symbol}@aggTrade/{self._symbol}@bookTicker"
        separator = "&" if "?" in self._ws_url else "?"
        return f"{self._ws_url}{separator}streams={streams}"

    async def run(self, stop_event: asyncio.Event | None = None) -> AsyncIterator[BtcTick]:
        """Yield :class:`BtcTick` values forever, reconnecting on failures."""
        websockets_mod = self._import_websockets()
        backoff = self._reconnect_backoff_seconds
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets_mod.connect(self.stream_url(), ssl=self._ssl_context) as websocket:
                    backoff = self._reconnect_backoff_seconds
                    async for raw_message in websocket:
                        if stop_event is not None and stop_event.is_set():
                            return
                        tick = self.parse_message(raw_message)
                        if tick is not None:
                            yield tick
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_backoff_max_seconds)

    def rest_price(self) -> BtcTick | None:
        try:
            response = self._http_client.get(self._rest_url, params={"symbol": self._symbol.upper()})
            response.raise_for_status()
            payload = response.json()
            price = float(payload.get("price") or 0.0)
            if price <= 0.0:
                return None
            return BtcTick(price=price, observed_at=datetime.now(timezone.utc), source="rest")
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    # Binance /api/v3/klines caps per-call results at 1000 rows. For 1440 × 1m
    # bars (24 h) we need pagination via the `endTime` parameter.
    _BINANCE_KLINES_MAX_PER_CALL = 1000

    def rest_klines(
        self,
        interval: str = "1m",
        limit: int = 1440,
        base_url: str = "https://api.binance.com/api/v3/klines",
    ) -> list[tuple[datetime, float, float]]:
        """Fetch historical kline bars as a list of ``(open_time, close, volume)``.

        Used once at daemon startup to seed the minute-bar buffer for HTF
        indicators (log-return over 1h / 4h / 24h, later RSI / EMA / VWAP).
        Paginates automatically when ``limit`` exceeds Binance's per-call cap
        of 1000 rows. On any failure we return whatever was fetched so far
        (possibly ``[]``) rather than raising — HTF features emit their
        defaults until enough bars accumulate.
        """
        remaining = max(1, int(limit))
        end_time_ms: int | None = None
        gathered: list[tuple[datetime, float, float]] = []
        while remaining > 0:
            page_limit = min(remaining, self._BINANCE_KLINES_MAX_PER_CALL)
            page = self._rest_klines_once(interval, page_limit, base_url, end_time_ms)
            if not page:
                break
            # Prepend so the final list is chronologically ascending and
            # contiguous even across pages.
            gathered = page + gathered
            remaining -= len(page)
            # Page back: next endTime is one ms before the earliest bar we got.
            earliest_ms = int(page[0][0].timestamp() * 1000)
            if end_time_ms is not None and earliest_ms >= end_time_ms:
                break  # defensive — shouldn't happen, avoids infinite loop
            end_time_ms = earliest_ms - 1
            if len(page) < page_limit:
                break  # source exhausted
        return gathered

    def _rest_klines_once(
        self,
        interval: str,
        limit: int,
        base_url: str,
        end_time_ms: int | None,
    ) -> list[tuple[datetime, float, float]]:
        params: dict[str, str | int] = {
            "symbol": self._symbol.upper(),
            "interval": interval,
            "limit": max(1, min(self._BINANCE_KLINES_MAX_PER_CALL, int(limit))),
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        try:
            response = self._http_client.get(base_url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return []
        if not isinstance(payload, list):
            return []
        bars: list[tuple[datetime, float, float]] = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                open_time_ms = int(row[0])
                close = float(row[4])
                volume = float(row[5])
            except (TypeError, ValueError):
                continue
            if close <= 0.0 or volume < 0.0:
                continue
            bars.append((_utc_from_ms(open_time_ms), close, volume))
        return bars

    @staticmethod
    def parse_message(raw_message: Any) -> BtcTick | None:
        if isinstance(raw_message, (bytes, bytearray)):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            envelope = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return None
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            data = envelope if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            return None
        stream = str(envelope.get("stream") or "") if isinstance(envelope, dict) else ""
        # aggTrade payload: {"e":"aggTrade","p":"<price>","q":"<qty>","T":<trade_time_ms>,...}
        if (data.get("e") == "aggTrade" or stream.endswith("@aggTrade")) and "p" in data:
            try:
                price = float(data["p"])
            except (TypeError, ValueError):
                return None
            try:
                quantity = float(data.get("q") or 0.0)
            except (TypeError, ValueError):
                quantity = 0.0
            return BtcTick(
                price=price,
                observed_at=_utc_from_ms(data.get("T") or data.get("E")),
                source="aggTrade",
                quantity=max(0.0, quantity),
            )
        # bookTicker payload: {"u":..., "s":"BTCUSDT", "b":"<bid>", "a":"<ask>", ...}
        if ("b" in data and "a" in data) or stream.endswith("@bookTicker"):
            try:
                bid = float(data["b"])
                ask = float(data["a"])
            except (TypeError, ValueError, KeyError):
                return None
            if bid <= 0.0 or ask <= 0.0:
                return None
            return BtcTick(
                price=(bid + ask) / 2.0,
                observed_at=datetime.now(timezone.utc),
                source="bookTicker",
            )
        return None

    @staticmethod
    def _import_websockets() -> Any:
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "websockets dependency is required for Binance BTC feed support."
            ) from exc
        return websockets
