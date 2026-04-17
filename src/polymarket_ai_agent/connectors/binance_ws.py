from __future__ import annotations

import asyncio
import json
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
    ):
        self._ws_url = ws_url
        self._rest_url = rest_url
        self._symbol = symbol.lower()
        self._reconnect_backoff_seconds = max(0.1, reconnect_backoff_seconds)
        self._reconnect_backoff_max_seconds = max(
            self._reconnect_backoff_seconds,
            reconnect_backoff_max_seconds,
        )
        self._http_client = http_client or httpx.Client(timeout=10)

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
                async with websockets_mod.connect(self.stream_url()) as websocket:
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
        # aggTrade payload: {"e":"aggTrade","p":"<price>","T":<trade_time_ms>,...}
        if (data.get("e") == "aggTrade" or stream.endswith("@aggTrade")) and "p" in data:
            try:
                price = float(data["p"])
            except (TypeError, ValueError):
                return None
            return BtcTick(
                price=price,
                observed_at=_utc_from_ms(data.get("T") or data.get("E")),
                source="aggTrade",
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
