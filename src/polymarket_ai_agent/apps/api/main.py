from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import uvicorn
from pydantic import BaseModel, Field
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse

from polymarket_ai_agent.apps.daemon.heartbeat import HeartbeatReader
from polymarket_ai_agent.config import (
    Settings,
    get_settings,
    get_effective_settings,
    runtime_settings_payload,
    save_runtime_overrides,
)
from polymarket_ai_agent.service import AgentService


def get_service() -> AgentService:
    return AgentService(get_effective_settings())


class SettingsUpdateRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class MarketActionRequest(BaseModel):
    market_id: str | None = None
    active: bool = True


class LiveWatchActionRequest(MarketActionRequest):
    iterations: int = Field(default=3, ge=1, le=100)
    interval_seconds: int = Field(default=2, ge=0, le=60)
    trade_limit: int = Field(default=20, ge=1, le=200)
    order_limit: int = Field(default=50, ge=1, le=500)


def create_app(
    service_factory: Callable[[], AgentService] = get_service,
    settings_factory: Callable[[], Settings] = get_effective_settings,
    base_settings_factory: Callable[[], Settings] = get_settings,
) -> FastAPI:
    snapshot_cache: dict[str, tuple[float, Any]] = {}

    def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        entry = snapshot_cache.get(key)
        if entry and (time.monotonic() - entry[0]) < ttl:
            return entry[1]
        result = fn()
        snapshot_cache[key] = (time.monotonic(), result)
        return result

    app = FastAPI(
        title="Polymarket AI Agent API",
        version="0.1.0",
        description="Read-only operator API for monitoring Polymarket agent state, decisions, and live diagnostics.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5180",
            "http://localhost:5180",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    def _collect_metrics(service: AgentService, settings: Settings) -> dict[str, Any]:
        reader = HeartbeatReader(settings.heartbeat_path)
        heartbeat = reader.read()
        heartbeat_age = reader.age_seconds()
        status_snapshot = service.status()
        db_size = service.journal.db_size_bytes()
        events_size = service.journal.events_jsonl_size_bytes()
        row_counts = service.portfolio.row_counts()
        exposure = service.portfolio.get_exposure_summary()
        return {
            "status": status_snapshot,
            "heartbeat": heartbeat,
            "heartbeat_age_seconds": heartbeat_age,
            "db_size_bytes": db_size,
            "events_jsonl_size_bytes": events_size,
            "row_counts": row_counts,
            "exposure": exposure,
            "open_positions": status_snapshot.get("open_positions", 0),
            "safety_stop_reason": status_snapshot.get("safety_stop_reason"),
            "daily_realized_pnl": status_snapshot.get("daily_realized_pnl", 0.0),
        }

    def _healthz_payload(service: AgentService, settings: Settings) -> dict[str, Any]:
        metrics = _collect_metrics(service, settings)
        heartbeat_age = metrics.get("heartbeat_age_seconds")
        status_snapshot = metrics["status"]
        heartbeat_stale = (
            heartbeat_age is None
            or heartbeat_age > float(settings.daemon_heartbeat_stale_seconds)
        )
        checks = {
            "db": {"ok": metrics["db_size_bytes"] >= 0},
            "heartbeat": {
                "ok": not heartbeat_stale,
                "age_seconds": heartbeat_age,
                "stale_threshold_seconds": float(settings.daemon_heartbeat_stale_seconds),
            },
            "safety_stop": {
                "ok": status_snapshot.get("safety_stop_reason") is None,
                "reason": status_snapshot.get("safety_stop_reason"),
            },
            "auth": {
                "ok": bool(status_snapshot.get("auth", {}).get("readonly_ready", False)),
                "detail": status_snapshot.get("auth", {}),
            },
        }
        ok = all(check.get("ok", False) for check in checks.values() if check is not None)
        return {
            "ok": ok,
            "checks": checks,
            "metrics_size": {
                "db_size_bytes": metrics["db_size_bytes"],
                "events_jsonl_size_bytes": metrics["events_jsonl_size_bytes"],
            },
        }

    def _format_prometheus(metrics: dict[str, Any]) -> str:
        def line(name: str, value: Any, labels: dict[str, str] | None = None, help_text: str | None = None) -> list[str]:
            out: list[str] = []
            if help_text is not None:
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} gauge")
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return []
            label_str = ""
            if labels:
                inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
                label_str = f"{{{inner}}}"
            out.append(f"{name}{label_str} {numeric}")
            return out

        lines: list[str] = []
        lines.extend(line("polymarket_agent_db_size_bytes", metrics["db_size_bytes"], help_text="SQLite database size on disk in bytes (includes WAL/SHM sidecars)."))
        lines.extend(line("polymarket_agent_events_jsonl_size_bytes", metrics["events_jsonl_size_bytes"], help_text="events.jsonl size on disk in bytes."))
        lines.extend(line("polymarket_agent_heartbeat_age_seconds", metrics.get("heartbeat_age_seconds") or -1, help_text="Seconds since the daemon last wrote its heartbeat; -1 if unavailable."))
        lines.extend(line("polymarket_agent_open_positions", metrics["open_positions"], help_text="Currently open positions."))
        lines.extend(line("polymarket_agent_daily_realized_pnl_usd", metrics["daily_realized_pnl"], help_text="Realized PnL for the current UTC day in USD."))
        lines.extend(line("polymarket_agent_long_btc_exposure_usd", metrics["exposure"]["long_btc_usd"], help_text="Notional long-BTC exposure in USD."))
        lines.extend(line("polymarket_agent_short_btc_exposure_usd", metrics["exposure"]["short_btc_usd"], help_text="Notional short-BTC exposure in USD."))
        lines.extend(line("polymarket_agent_net_btc_exposure_usd", metrics["exposure"]["net_btc_usd"], help_text="Signed net BTC directional exposure in USD."))
        for table, value in metrics["row_counts"].items():
            lines.extend(line("polymarket_agent_db_rows", value, labels={"table": table}, help_text="Row count per SQLite table."))
        heartbeat = metrics.get("heartbeat") or {}
        daemon_metrics = (heartbeat or {}).get("metrics") or {}
        for key in (
            "polymarket_events",
            "btc_ticks",
            "decision_ticks",
            "decision_skips_busy",
            "discovery_cycles",
            "discovery_errors",
            "active_market_count",
            "last_decision_latency_ms",
        ):
            if key in daemon_metrics:
                lines.extend(
                    line(
                        f"polymarket_agent_{key}",
                        daemon_metrics[key],
                        help_text=f"Daemon runtime metric: {key} (from heartbeat).",
                    )
                )
        triggers = daemon_metrics.get("decision_triggers") or {}
        if isinstance(triggers, dict):
            for trigger, count in triggers.items():
                lines.extend(
                    line(
                        "polymarket_agent_decision_triggers",
                        count,
                        labels={"reason": str(trigger)},
                        help_text="Decision ticks that fired, broken down by trigger reason (Polymarket WS event type).",
                    )
                )
        safety = metrics.get("safety_stop_reason")
        lines.extend(line("polymarket_agent_safety_stop_triggered", 0 if safety is None else 1, help_text="1 if a safety stop has fired, 0 otherwise."))
        return "\n".join(lines) + "\n"

    @app.get("/api/healthz")
    def api_healthz(service: AgentService = Depends(service_factory), settings: Settings = Depends(settings_factory)) -> dict:
        return _healthz_payload(service, settings)

    @app.get("/api/metrics")
    def api_metrics(
        service: AgentService = Depends(service_factory),
        settings: Settings = Depends(settings_factory),
        format: str = Query("json", pattern="^(json|prometheus)$"),
    ):
        metrics = _collect_metrics(service, settings)
        if format == "prometheus":
            return PlainTextResponse(
                content=_format_prometheus(metrics),
                media_type="text/plain; version=0.0.4",
            )
        return metrics

    def resolve_active_market_id(service: AgentService, market_id: str | None, active: bool) -> str | None:
        if market_id:
            return market_id
        if not active:
            return None
        try:
            return service.get_active_market_id()
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def safe_live_activity(service: AgentService) -> dict:
        try:
            # ``skip_scoring=True`` pulls the preflight assessment from the
            # most recent ``daemon_tick`` instead of re-invoking the scoring
            # engine — which would round-trip to OpenRouter when the key is
            # set — on every dashboard poll.
            return service.live_activity(skip_scoring=True)
        except Exception as exc:
            return {
                "readonly": True,
                "market_id": "",
                "auth": _cached("auth", 60.0, service.auth_status),
                "preflight": {
                    "blockers": ["no_active_market"],
                    "market": {
                        "question": "No active market matched the configured market family.",
                        "implied_probability": 0.0,
                        "liquidity_usd": 0.0,
                        "seconds_to_expiry": 0,
                    },
                    "assessment": {
                        "fair_probability": 0.0,
                        "confidence": 0.0,
                        "edge": 0.0,
                        "suggested_side": "ABSTAIN",
                    },
                },
                "last_poll": {
                    "polled_at": "",
                    "time_remaining_seconds": 0,
                    "time_remaining_minutes": 0.0,
                    "market_trade_count": 0,
                    "trade_counts": {"yes": 0, "no": 0, "other": 0, "total": 0},
                },
                "open_orders": {"count": 0, "orders": []},
                "tracked_orders": {"count": 0, "active_count": 0, "terminal_count": 0, "orders": []},
                "recent_trades": {"count": 0, "trades": []},
                "error": str(exc),
            }

    def _daemon_heartbeat_payload(settings: Settings) -> dict:
        reader = HeartbeatReader(settings.heartbeat_path)
        return {
            "age_seconds": reader.age_seconds(),
            "heartbeat": reader.read(),
        }

    def _pending_makers_payload(settings: Settings) -> dict:
        """Resting paper-maker limits sourced from the heartbeat.

        The daemon owns ``_pending_makers`` in process memory; the API has
        no direct view, so we surface them via the heartbeat field the
        daemon writes on every cycle. Empty list if heartbeat is missing.
        """
        reader = HeartbeatReader(settings.heartbeat_path)
        heartbeat = reader.read() or {}
        return {"orders": heartbeat.get("pending_makers", [])}

    def _latest_daemon_ticks(service: AgentService) -> dict:
        # read_recent_events returns file-order (oldest first); iterate newest-first
        # so "first occurrence per (strategy_id, market_id)" is the most recent
        # tick that scorer emitted for that market. De-duping by market_id alone
        # would collapse a multi-strategy run to one tick per market and hide
        # fade / adaptive behind whichever scorer fired last.
        #
        # Scan a wide window (~hours of runtime) so MarketCell hover + link works on
        # older closed positions whose markets are no longer actively tracked.
        events = service.journal.read_recent_events(limit=5000)
        seen: set[tuple[str, str]] = set()
        ticks: list[dict] = []
        for e in reversed(events):
            if e.get("event_type") != "daemon_tick":
                continue
            payload = e.get("payload", {})
            mid = str(payload.get("market_id", ""))
            strategy_id = str(payload.get("strategy_id") or "fade")
            key = (strategy_id, mid)
            if mid and key not in seen:
                seen.add(key)
                ticks.append(payload)
        return {"ticks": ticks}

    def build_dashboard_snapshot(service: AgentService) -> dict:
        return {
            "status": service.status(),
            "auth": _cached("auth", 60.0, service.auth_status),
            "settings": runtime_settings_payload(service.settings),
            "live_activity": _cached("live_activity", 30.0, lambda: safe_live_activity(service)),
            "portfolio_summary": portfolio_summary(service=service),
            "open_positions": open_positions(service=service),
            "closed_positions": closed_positions(limit=100, service=service),
            "equity_curve": equity_curve(limit=200, service=service),
            "report": _cached("report", 60.0, lambda: report(session_id=None, service=service)),
            "recent_events": recent_events(limit=12, service=service),
            "recent_decisions": recent_decisions(limit=500, window_seconds=600, service=service),
            "live_orders": _cached("live_orders", 30.0, lambda: live_orders(service=service)),
            "live_trades": _cached("live_trades", 30.0, lambda: live_trades(limit=20, service=service)),
            "daemon_heartbeat": _daemon_heartbeat_payload(service.settings),
            "daemon_ticks": _latest_daemon_ticks(service),
            "paper_activity": paper_activity(limit=30, service=service),
            "pending_makers": _pending_makers_payload(service.settings),
        }

    def streamable_dashboard_sections(service: AgentService) -> dict:
        snapshot = build_dashboard_snapshot(service)
        return {
            "status": snapshot["status"],
            "auth": snapshot["auth"],
            "settings": snapshot["settings"],
            "live_activity": snapshot["live_activity"],
            "portfolio_summary": snapshot["portfolio_summary"],
            "open_positions": snapshot["open_positions"],
            "closed_positions": snapshot["closed_positions"],
            "equity_curve": snapshot["equity_curve"],
            "report": snapshot["report"],
            "recent_events": snapshot["recent_events"],
            "recent_decisions": snapshot["recent_decisions"],
            "live_orders": snapshot["live_orders"],
            "live_trades": snapshot["live_trades"],
            "daemon_heartbeat": snapshot["daemon_heartbeat"],
            "daemon_ticks": snapshot["daemon_ticks"],
            "paper_activity": snapshot["paper_activity"],
            "pending_makers": snapshot["pending_makers"],
        }

    @app.get("/api/status")
    def status(service: AgentService = Depends(service_factory)) -> dict:
        return service.status()

    @app.get("/api/auth")
    def auth(service: AgentService = Depends(service_factory)) -> dict:
        return service.auth_status()

    @app.get("/api/settings")
    def settings_snapshot() -> dict:
        return runtime_settings_payload(settings_factory())

    @app.put("/api/settings")
    def update_settings(
        body: SettingsUpdateRequest,
        service: AgentService = Depends(service_factory),
    ) -> dict:
        base_settings = base_settings_factory()
        # save_runtime_overrides writes one settings_changes row per changed
        # field (source='api'). We also emit a mirror api_settings_write
        # event so the audit timeline distinguishes operator intent (this
        # event) from daemon-observed effect (the settings_changed event
        # the reload loop emits once it picks up the rows).
        store = getattr(service, "settings_store", None)
        last_id_before = store.get_max_id() if store is not None else 0
        save_runtime_overrides(base_settings, body.values)
        new_ids: list[int] = []
        if store is not None:
            new_ids = [row.id for row in store.list_changes(since_id=last_id_before)]
        try:
            service.journal.log_event(
                "api_settings_write",
                {"source": "api", "received": dict(body.values), "row_ids": new_ids},
            )
        except Exception:
            # Journal failures never break a settings write.
            pass
        return runtime_settings_payload(settings_factory())

    @app.get("/api/markets")
    def markets(limit: int = Query(10, ge=1, le=100), service: AgentService = Depends(service_factory)) -> dict:
        discovered = service.discover_markets()[:limit]
        return {
            "count": len(discovered),
            "markets": [
                {
                    "market_id": market.market_id,
                    "question": market.question,
                    "slug": market.slug,
                    "implied_probability": market.implied_probability,
                    "liquidity_usd": market.liquidity_usd,
                    "volume_24h_usd": market.volume_24h_usd,
                    "end_date_iso": market.end_date_iso,
                }
                for market in discovered
            ],
        }

    @app.get("/api/doctor")
    def doctor(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, market_id, active)
        return service.doctor(resolved_market_id or None)

    @app.get("/api/live/activity")
    def live_activity(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        trade_limit: int = Query(20, ge=1, le=200),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, market_id, active)
        return service.live_activity(resolved_market_id or None, trade_limit=trade_limit)

    @app.get("/api/live/reconcile")
    def live_reconcile(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        trade_limit: int = Query(20, ge=1, le=200),
        order_limit: int = Query(50, ge=1, le=500),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, market_id, active)
        return service.live_reconcile(resolved_market_id or None, trade_limit=trade_limit, order_limit=order_limit)

    @app.get("/api/report")
    def report(session_id: str | None = Query(default=None), service: AgentService = Depends(service_factory)) -> dict:
        generated = service.generate_operator_report(session_id or None)
        return {
            "session_id": generated.session_id,
            "generated_at": generated.generated_at.isoformat(),
            "summary": generated.summary,
            "items": generated.items,
        }

    @app.get("/api/decisions/recent")
    def recent_decisions(
        limit: int = Query(50, ge=1, le=5000),
        window_seconds: int | None = Query(None, ge=1, le=3600),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        """Return ``daemon_tick`` events for the Signal History panel.

        When ``window_seconds`` is set, filter to events logged within that
        many seconds of now and cap at ``limit`` entries. Without the window
        we keep the legacy "last ``limit`` ticks" behaviour so unqualified
        callers see the same shape.

        Multi-strategy runs emit ~2× the tick volume (one tick per scorer),
        so ``window_seconds=600`` can return 2-3k ticks on a busy feed; scan
        a wide journal slice so nothing inside the window is missed.
        """
        # read_recent_events returns oldest→newest; take the TAIL so the top
        # of Signal History reflects the latest tick, not the newest-of-oldest.
        if window_seconds is None:
            events = [e for e in service.journal.read_recent_events(limit=limit * 6) if e["event_type"] == "daemon_tick"]
            return {
                "count": len(events[-limit:]),
                "decisions": events[-limit:],
            }

        # Time-windowed path: scan a generously wide journal slice and filter
        # by logged_at ≥ cutoff. At ~10 ticks/second on a 3-market soak this
        # is 6k ticks for a 10-minute window; 20k scan gives headroom.
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        scan_limit = max(limit * 20, 20000)
        window_events: list[dict] = []
        for event in service.journal.read_recent_events(limit=scan_limit):
            if event.get("event_type") != "daemon_tick":
                continue
            ts_raw = str(event.get("logged_at") or "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            window_events.append(event)
        # Keep the newest ``limit`` entries; oldest-first already; take TAIL.
        trimmed = window_events[-limit:]
        return {
            "count": len(trimmed),
            "decisions": trimmed,
            "window_seconds": window_seconds,
        }

    @app.get("/api/paper/activity")
    def paper_activity(limit: int = Query(30, ge=1, le=200), service: AgentService = Depends(service_factory)) -> dict:
        """Recent execution_result events — paper (and live) fills from the journal.

        Unique to paper runs since Polymarket has no orders to list; complements
        the Portfolio tab (position-centric) with an execution-centric view.

        Scan window is deliberately wide (3000 events) because execution_result
        events are rare compared to daemon_tick; a narrow window misses them.
        """
        scan_limit = max(limit * 20, 3000)
        events = [e for e in service.journal.read_recent_events(limit=scan_limit) if e["event_type"] == "execution_result"]
        return {
            "count": len(events[-limit:]),
            "events": events[-limit:],
        }

    @app.get("/api/live/orders")
    def live_orders(service: AgentService = Depends(service_factory)) -> dict:
        return service.live_orders()

    @app.get("/api/live/trades")
    def live_trades(
        market_id: str | None = Query(default=None),
        limit: int = Query(20, ge=1, le=200),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        return service.live_trades(market_id=market_id, limit=limit)

    @app.get("/api/events/recent")
    def recent_events(limit: int = Query(20, ge=1, le=200), service: AgentService = Depends(service_factory)) -> dict:
        events = service.journal.read_recent_events(limit=limit)
        return {
            "count": len(events),
            "events": events,
        }

    @app.get("/api/positions/timeline")
    def position_timeline(
        order_id: str = Query(...),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        """Reconstruct the full lifecycle of a single closed position.

        Returns:
        - all DB position rows sharing this ``order_id`` (TP-ladder splits
          produce multiple rows from one fill)
        - chronologically ordered journal events for the same market in a
          window from ~5 minutes before the fill to ~1 minute after the
          last close: maker placements/cancels, the fill itself,
          position closes
        - derived stats (total realized PnL, total notional, hold seconds,
          ROI) so the UI can render them without re-deriving

        The order_id encodes the maker-placement Unix timestamp (format:
        ``paper-maker-{strategy_id}-{market_id}-{ts}``) — we use that
        as the lower bound of the event scan window.
        """
        from datetime import datetime, timezone, timedelta

        def _to_dt(v) -> datetime | None:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            if isinstance(v, str):
                try:
                    return datetime.fromisoformat(v)
                except ValueError:
                    return None
            return None

        # 1. Resolve the position(s) from the DB.
        all_closed = service.portfolio.list_closed_positions(limit=2000)
        rows = [p for p in all_closed if getattr(p, "order_id", "") == order_id]
        if not rows:
            return {"order_id": order_id, "found": False, "rows": [], "events": [], "stats": None}

        market_id = rows[0].market_id
        strategy_id = getattr(rows[0], "strategy_id", "fade") or "fade"

        # 2. Compute the scan window. Try to decode placed_at from the
        # synthetic order_id; fall back to opened_at - 5min if the format
        # doesn't match (e.g., a real CLOB order_id).
        scan_start: datetime | None = None
        if order_id.startswith("paper-maker-"):
            tail = order_id.rsplit("-", 1)[-1]
            try:
                placed_ts = int(tail)
                scan_start = datetime.fromtimestamp(placed_ts, tz=timezone.utc) - timedelta(seconds=30)
            except ValueError:
                scan_start = None
        opened_at_dt = _to_dt(rows[0].opened_at) or datetime.now(timezone.utc)
        if scan_start is None:
            scan_start = opened_at_dt - timedelta(minutes=5)
        # Latest close among the rows + 60s buffer.
        closed_dts = [_to_dt(p.closed_at) for p in rows]
        closed_dts = [d for d in closed_dts if d is not None]
        latest_closed_at = max(closed_dts) if closed_dts else opened_at_dt
        scan_end = latest_closed_at + timedelta(seconds=60)

        # 3. Walk events.jsonl. Only filter by market_id + time window —
        # the UI decides how to render each event_type.
        events_path = service.settings.events_path
        timeline_events: list[dict] = []
        if events_path.exists():
            with events_path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    et = e.get("event_type")
                    if et not in (
                        "paper_maker_placed",
                        "paper_maker_cancelled",
                        "execution_result",
                        "position_closed",
                    ):
                        continue
                    p = e.get("payload") or {}
                    if str(p.get("market_id") or "") != market_id:
                        continue
                    ts = e.get("logged_at") or ""
                    try:
                        ts_dt = datetime.fromisoformat(ts)
                    except (ValueError, TypeError):
                        continue
                    if ts_dt < scan_start or ts_dt > scan_end:
                        continue
                    # For execution_result, only include the one matching
                    # this order_id (others would be unrelated trades on
                    # the same market). Other event types don't carry the
                    # synthetic order_id so we keep them all.
                    if et == "execution_result" and str(p.get("order_id") or "") != order_id:
                        continue
                    timeline_events.append(e)
        timeline_events.sort(key=lambda x: x.get("logged_at") or "")

        # 4. Stats.
        total_realized = sum(float(p.realized_pnl) for p in rows)
        total_size = sum(float(p.size_usd) for p in rows)
        roi_pct = (total_realized / total_size) if total_size > 0 else 0.0
        hold_seconds = (latest_closed_at - opened_at_dt).total_seconds()

        return {
            "order_id": order_id,
            "found": True,
            "market_id": market_id,
            "strategy_id": strategy_id,
            "rows": [
                {
                    "market_id": p.market_id,
                    "side": p.side,
                    "size_usd": p.size_usd,
                    "entry_price": p.entry_price,
                    "exit_price": p.exit_price,
                    "realized_pnl": p.realized_pnl,
                    "close_reason": p.close_reason,
                    "opened_at": (_to_dt(p.opened_at) or datetime.now(timezone.utc)).isoformat(),
                    "closed_at": _to_dt(p.closed_at).isoformat() if _to_dt(p.closed_at) else None,
                    "fees_paid": getattr(p, "fees_paid", 0.0),
                }
                for p in rows
            ],
            "events": timeline_events,
            "stats": {
                "total_realized_pnl": round(total_realized, 4),
                "total_size_usd": round(total_size, 4),
                "roi_pct": round(roi_pct, 4),
                "hold_seconds": round(hold_seconds, 1),
                "tranches": len(rows),
            },
        }

    @app.get("/api/events/stream")
    async def stream_events(
        limit: int = Query(12, ge=1, le=100),
        interval_seconds: int = Query(5, ge=1, le=60),
        service: AgentService = Depends(service_factory),
    ) -> StreamingResponse:
        async def event_generator():
            previous_payload = ""
            while True:
                events = service.journal.read_recent_events(limit=limit)
                payload = json.dumps({"count": len(events), "events": events})
                if payload != previous_payload:
                    previous_payload = payload
                    yield f"event: recent_events\ndata: {payload}\n\n"
                await asyncio.sleep(interval_seconds)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/api/portfolio/summary")
    def portfolio_summary(service: AgentService = Depends(service_factory)) -> dict:
        open_positions = service.portfolio.list_open_positions()
        # Closed-position counts and per-strategy PnL come from a SQL
        # GROUP BY over the full table — the prior implementation
        # materialised at most 200 rows and silently truncated counts
        # once the soak passed that mark.
        closed_stats = service.portfolio.get_closed_position_stats()
        per_strategy: dict[str, dict] = {}
        for stats in closed_stats:
            sid = stats["strategy_id"] or "fade"
            per_strategy[sid] = {
                "strategy_id": sid,
                "open_positions": 0, "closed_positions": stats["closed_positions"],
                "total_realized_pnl": stats["total_realized_pnl"],
                "open_notional": 0.0,
                "wins": stats["wins"], "losses": stats["losses"],
            }
        for pos in open_positions:
            sid = getattr(pos, "strategy_id", "fade") or "fade"
            bucket = per_strategy.setdefault(sid, {
                "strategy_id": sid,
                "open_positions": 0, "closed_positions": 0,
                "total_realized_pnl": 0.0, "open_notional": 0.0,
                "wins": 0, "losses": 0,
            })
            bucket["open_positions"] += 1
            bucket["open_notional"] += pos.size_usd
        for bucket in per_strategy.values():
            decided = bucket["wins"] + bucket["losses"]
            bucket["win_rate"] = (bucket["wins"] / decided) if decided else None
            bucket["total_realized_pnl"] = round(bucket["total_realized_pnl"], 6)
            bucket["open_notional"] = round(bucket["open_notional"], 4)
        total_closed = sum(s["closed_positions"] for s in closed_stats)
        return {
            "open_positions": len(open_positions),
            "closed_positions": total_closed,
            "total_realized_pnl": service.portfolio.get_total_realized_pnl(),
            "daily_realized_pnl": service.portfolio.get_daily_realized_pnl(),
            "open_position_notional": round(sum(position.size_usd for position in open_positions), 4),
            "per_strategy": sorted(per_strategy.values(), key=lambda b: b["strategy_id"]),
        }

    @app.get("/api/portfolio/open-positions")
    def open_positions(service: AgentService = Depends(service_factory)) -> dict:
        positions = service.portfolio.list_open_positions()
        items = [
            {
                "market_id": p.market_id,
                "side": p.side.value,
                "size_usd": p.size_usd,
                "entry_price": p.entry_price,
                "opened_at": p.opened_at.isoformat(),
                "order_id": p.order_id,
                "strategy_id": p.strategy_id,
            }
            for p in positions
        ]
        return {"count": len(items), "positions": items}

    @app.get("/api/portfolio/closed-positions")
    def closed_positions(limit: int = Query(100, ge=1, le=500), service: AgentService = Depends(service_factory)) -> dict:
        positions = service.portfolio.list_closed_positions(limit=limit)
        fee_bps = float(service.settings.fee_bps)
        cumulative = 0.0
        items = []
        for position in reversed(positions):
            cumulative += position.realized_pnl
            # Round-trip fee estimate on this tranche's size. Back-computed from the
            # current fee_bps setting — accurate when fee_bps has been stable over
            # the position's lifetime (typically the case).
            fees_paid = round(position.size_usd * (fee_bps / 10_000.0) * 2.0, 6) if fee_bps > 0 else 0.0
            items.append(
                {
                    "market_id": position.market_id,
                    "order_id": position.order_id,
                    "side": position.side.value,
                    "size_usd": position.size_usd,
                    "entry_price": position.entry_price,
                    "exit_price": position.exit_price,
                    "opened_at": position.opened_at.isoformat(),
                    "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                    "close_reason": position.close_reason,
                    "realized_pnl": position.realized_pnl,
                    "fees_paid": fees_paid,
                    "cumulative_pnl": round(cumulative, 6),
                    "strategy_id": position.strategy_id,
                }
            )
        return {"count": len(items), "positions": items}

    @app.get("/api/portfolio/equity-curve")
    def equity_curve(limit: int = Query(200, ge=1, le=1000), service: AgentService = Depends(service_factory)) -> dict:
        positions = list(reversed(service.portfolio.list_closed_positions(limit=limit)))
        points = []
        cumulative = 0.0
        for index, position in enumerate(positions, start=1):
            cumulative += position.realized_pnl
            points.append(
                {
                    "sequence": index,
                    "market_id": position.market_id,
                    "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                    "realized_pnl": position.realized_pnl,
                    "equity": round(cumulative, 6),
                }
            )
        return {"count": len(points), "points": points}

    @app.get("/api/dashboard")
    def dashboard(service: AgentService = Depends(service_factory)) -> dict:
        return build_dashboard_snapshot(service)

    @app.get("/api/dashboard/stream")
    async def dashboard_stream(
        interval_seconds: int = Query(5, ge=1, le=60),
    ) -> StreamingResponse:
        async def event_generator():
            previous_sections: dict[str, str] = {}
            while True:
                service = service_factory()
                for event_name, payload_obj in streamable_dashboard_sections(service).items():
                    payload = json.dumps(payload_obj)
                    if previous_sections.get(event_name) != payload:
                        previous_sections[event_name] = payload
                        yield f"event: {event_name}\ndata: {payload}\n\n"
                await asyncio.sleep(interval_seconds)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/api/simulate")
    def simulate(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, market_id, active)
        if not resolved_market_id:
            raise HTTPException(status_code=400, detail="Provide market_id or set active=true.")
        snapshot, assessment, decision = service.simulate_market(resolved_market_id)
        return {
            "market_id": resolved_market_id,
            "readonly": True,
            "question": snapshot.candidate.question,
            "assessment": {
                "fair_probability": assessment.fair_probability,
                "confidence": assessment.confidence,
                "edge": assessment.edge,
                "suggested_side": assessment.suggested_side.value,
            },
            "decision": {
                "status": decision.status.value,
                "side": decision.side.value,
                "size_usd": decision.size_usd,
                "limit_price": decision.limit_price,
                "rejected_by": decision.rejected_by,
            },
        }

    @app.post("/api/actions/simulate-active")
    def simulate_active_action(
        body: MarketActionRequest,
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, body.market_id, body.active)
        if not resolved_market_id:
            raise HTTPException(status_code=400, detail="Provide market_id or set active=true.")
        snapshot, assessment, decision = service.simulate_market(resolved_market_id)
        return {
            "action": "simulate-active",
            "market_id": resolved_market_id,
            "readonly": True,
            "question": snapshot.candidate.question,
            "assessment": {
                "fair_probability": assessment.fair_probability,
                "confidence": assessment.confidence,
                "edge": assessment.edge,
                "suggested_side": assessment.suggested_side.value,
            },
            "decision": {
                "status": decision.status.value,
                "side": decision.side.value,
                "size_usd": decision.size_usd,
                "limit_price": decision.limit_price,
                "rejected_by": decision.rejected_by,
            },
        }

    @app.post("/api/actions/live-preflight")
    def live_preflight_action(
        body: MarketActionRequest,
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, body.market_id, body.active)
        return service.live_preflight(resolved_market_id or None)

    @app.post("/api/actions/live-reconcile")
    def live_reconcile_action(
        body: MarketActionRequest,
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, body.market_id, body.active)
        return service.live_reconcile(resolved_market_id or None)

    @app.get("/api/daemon/heartbeat")
    def daemon_heartbeat(settings: Settings = Depends(settings_factory)) -> dict:
        return _daemon_heartbeat_payload(settings)

    @app.post("/api/actions/live-watch")
    async def live_watch_action(
        body: LiveWatchActionRequest,
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = resolve_active_market_id(service, body.market_id, body.active)
        cycles: list[dict] = []
        previous = None
        for idx in range(body.iterations):
            cycle = service.live_reconcile(
                resolved_market_id or None,
                trade_limit=body.trade_limit,
                order_limit=body.order_limit,
            )
            fingerprint = {
                "blockers": cycle["preflight"].get("blockers", []),
                "tracked_summary": cycle["tracked_orders"].get("summary", {}),
                "recent_trade_count": cycle["recent_trades"].get("count", 0),
            }
            cycle["changed"] = fingerprint != previous
            previous = fingerprint
            cycles.append(cycle)
            if idx < body.iterations - 1 and body.interval_seconds > 0:
                await asyncio.sleep(body.interval_seconds)
        return {
            "action": "live-watch",
            "readonly": True,
            "market_id": resolved_market_id,
            "iterations_requested": body.iterations,
            "iterations_completed": len(cycles),
            "cycles": cycles,
        }

    return app


app = create_app()


def run() -> None:
    uvicorn.run("polymarket_ai_agent.apps.api.main:app", host="127.0.0.1", port=8000, reload=False)
