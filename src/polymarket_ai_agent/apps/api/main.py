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
            return service.live_activity()
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

    def _latest_daemon_ticks(service: AgentService) -> dict:
        # read_recent_events returns file-order (oldest first); iterate newest-first
        # so "first occurrence per market_id" is the most recent tick for that market.
        events = service.journal.read_recent_events(limit=200)
        seen: set[str] = set()
        ticks: list[dict] = []
        for e in reversed(events):
            if e.get("event_type") != "daemon_tick":
                continue
            mid = e.get("payload", {}).get("market_id", "")
            if mid and mid not in seen:
                seen.add(mid)
                ticks.append(e.get("payload", {}))
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
            "recent_decisions": recent_decisions(limit=50, service=service),
            "live_orders": _cached("live_orders", 30.0, lambda: live_orders(service=service)),
            "live_trades": _cached("live_trades", 30.0, lambda: live_trades(limit=20, service=service)),
            "daemon_heartbeat": _daemon_heartbeat_payload(service.settings),
            "daemon_ticks": _latest_daemon_ticks(service),
            "paper_activity": paper_activity(limit=30, service=service),
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
    def update_settings(body: SettingsUpdateRequest) -> dict:
        base_settings = base_settings_factory()
        save_runtime_overrides(base_settings, body.values)
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
    def recent_decisions(limit: int = Query(50, ge=1, le=200), service: AgentService = Depends(service_factory)) -> dict:
        events = [e for e in service.journal.read_recent_events(limit=limit * 6) if e["event_type"] == "daemon_tick"]
        return {
            "count": len(events[:limit]),
            "decisions": events[:limit],
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
        closed_positions = service.portfolio.list_closed_positions(limit=200)
        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "total_realized_pnl": service.portfolio.get_total_realized_pnl(),
            "daily_realized_pnl": service.portfolio.get_daily_realized_pnl(),
            "open_position_notional": round(sum(position.size_usd for position in open_positions), 4),
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
            }
            for p in positions
        ]
        return {"count": len(items), "positions": items}

    @app.get("/api/portfolio/closed-positions")
    def closed_positions(limit: int = Query(100, ge=1, le=500), service: AgentService = Depends(service_factory)) -> dict:
        positions = service.portfolio.list_closed_positions(limit=limit)
        cumulative = 0.0
        items = []
        for position in reversed(positions):
            cumulative += position.realized_pnl
            items.append(
                {
                    "market_id": position.market_id,
                    "side": position.side.value,
                    "size_usd": position.size_usd,
                    "entry_price": position.entry_price,
                    "exit_price": position.exit_price,
                    "opened_at": position.opened_at.isoformat(),
                    "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                    "close_reason": position.close_reason,
                    "realized_pnl": position.realized_pnl,
                    "cumulative_pnl": round(cumulative, 6),
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
