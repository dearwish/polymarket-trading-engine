from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MarketStreamEvent:
    event_type: str
    payload: dict[str, Any]


class PolymarketMarketStream:
    """Public market-channel websocket client with auto-reconnect.

    Consumers iterate over :py:meth:`run` which keeps a connection open, yields
    parsed events, and re-subscribes with exponential backoff on disconnects.
    The standalone :py:meth:`subscribe` coroutine is retained for single-shot
    tests and ad-hoc tooling.
    """

    def __init__(
        self,
        url: str,
        reconnect_backoff_seconds: float = 2.0,
        reconnect_backoff_max_seconds: float = 30.0,
        ssl_verify: bool = True,
    ):
        self.url = url
        self._reconnect_backoff_seconds = max(0.1, reconnect_backoff_seconds)
        self._reconnect_backoff_max_seconds = max(
            self._reconnect_backoff_seconds,
            reconnect_backoff_max_seconds,
        )
        self._ssl_context: ssl.SSLContext | bool = ssl_verify or self._make_insecure_ctx()

    @staticmethod
    def _make_insecure_ctx() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def subscribe(self, asset_ids: Iterable[str]) -> AsyncIterator[MarketStreamEvent]:
        asset_list = [asset_id for asset_id in asset_ids if asset_id]
        if not asset_list:
            return
        websockets_mod = self._import_websockets()
        async with websockets_mod.connect(self.url, ssl=self._ssl_context) as websocket:
            await websocket.send(self._subscription_payload(asset_list))
            async for raw_message in websocket:
                for event in self._parse_messages(raw_message):
                    yield event

    async def run(
        self,
        asset_ids: Iterable[str],
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[MarketStreamEvent]:
        """Run forever, yielding events across reconnects.

        ``stop_event`` allows the caller to cooperatively exit the loop; if it
        is set between messages the generator returns cleanly.
        """
        asset_list = [asset_id for asset_id in asset_ids if asset_id]
        if not asset_list:
            return
        websockets_mod = self._import_websockets()
        backoff = self._reconnect_backoff_seconds
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets_mod.connect(self.url, ssl=self._ssl_context) as websocket:
                    await websocket.send(self._subscription_payload(asset_list))
                    backoff = self._reconnect_backoff_seconds
                    async for raw_message in websocket:
                        if stop_event is not None and stop_event.is_set():
                            return
                        for event in self._parse_messages(raw_message):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_backoff_max_seconds)

    def _subscription_payload(self, asset_list: list[str]) -> str:
        return json.dumps(
            {
                "assets_ids": asset_list,
                "type": "market",
                "custom_feature_enabled": True,
            }
        )

    @staticmethod
    def _import_websockets() -> Any:
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "websockets dependency is required for Polymarket market stream support."
            ) from exc
        return websockets

    @classmethod
    def _parse_messages(cls, raw_message: Any) -> list[MarketStreamEvent]:
        if isinstance(raw_message, (bytes, bytearray)):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return []
        event = cls.parse_message(raw_message)
        if event is not None:
            return [event]
        try:
            payload = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return []
        if isinstance(payload, list):
            return [e for item in payload if (e := cls._parse_mapping(item)) is not None]
        return []

    @staticmethod
    def parse_message(raw_message: str) -> MarketStreamEvent | None:
        try:
            payload = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return None
        return PolymarketMarketStream._parse_mapping(payload)

    @staticmethod
    def _parse_mapping(payload: Any) -> MarketStreamEvent | None:
        if not isinstance(payload, dict):
            return None
        event_type = str(payload.get("event_type") or "")
        if not event_type:
            return None
        return MarketStreamEvent(event_type=event_type, payload=payload)


class PolymarketUserStream(PolymarketMarketStream):
    """User-channel websocket client for authenticated fill/cancel updates.

    Expects auth credentials (key/secret/passphrase) derived from the CLOB
    client. The subscription payload follows Polymarket's documented user
    channel shape. Parsing and reconnect behavior are inherited from
    :class:`PolymarketMarketStream`.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        markets: Iterable[str] | None = None,
        reconnect_backoff_seconds: float = 2.0,
        reconnect_backoff_max_seconds: float = 30.0,
    ):
        super().__init__(
            url,
            reconnect_backoff_seconds=reconnect_backoff_seconds,
            reconnect_backoff_max_seconds=reconnect_backoff_max_seconds,
        )
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._markets = [m for m in (markets or []) if m]

    def _subscription_payload(self, asset_list: list[str]) -> str:  # type: ignore[override]
        return json.dumps(
            {
                "type": "user",
                "auth": {
                    "apiKey": self._api_key,
                    "secret": self._api_secret,
                    "passphrase": self._api_passphrase,
                },
                "markets": self._markets or asset_list,
            }
        )
