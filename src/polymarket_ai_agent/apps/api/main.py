from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from polymarket_ai_agent.config import get_settings
from polymarket_ai_agent.service import AgentService


def get_service() -> AgentService:
    return AgentService(get_settings())


def create_app(service_factory: Callable[[], AgentService] = get_service) -> FastAPI:
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

    @app.get("/api/status")
    def status(service: AgentService = Depends(service_factory)) -> dict:
        return service.status()

    @app.get("/api/auth")
    def auth(service: AgentService = Depends(service_factory)) -> dict:
        return service.auth_status()

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
        resolved_market_id = market_id
        if not resolved_market_id and active:
            resolved_market_id = service.get_active_market_id()
        return service.doctor(resolved_market_id or None)

    @app.get("/api/live/activity")
    def live_activity(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        trade_limit: int = Query(20, ge=1, le=200),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = market_id
        if not resolved_market_id and active:
            resolved_market_id = service.get_active_market_id()
        return service.live_activity(resolved_market_id or None, trade_limit=trade_limit)

    @app.get("/api/live/reconcile")
    def live_reconcile(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        trade_limit: int = Query(20, ge=1, le=200),
        order_limit: int = Query(50, ge=1, le=500),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = market_id
        if not resolved_market_id and active:
            resolved_market_id = service.get_active_market_id()
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
    def recent_decisions(limit: int = Query(25, ge=1, le=200), service: AgentService = Depends(service_factory)) -> dict:
        allowed = {"simulation_cycle", "simulation_decision", "trade_decision", "market_assessment"}
        events = [event for event in service.journal.read_recent_events(limit=limit * 4) if event["event_type"] in allowed]
        return {
            "count": len(events[:limit]),
            "decisions": events[:limit],
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

    @app.get("/api/simulate")
    def simulate(
        market_id: str | None = Query(default=None),
        active: bool = Query(default=True),
        service: AgentService = Depends(service_factory),
    ) -> dict:
        resolved_market_id = market_id
        if not resolved_market_id and active:
            resolved_market_id = service.get_active_market_id()
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

    return app


app = create_app()


def run() -> None:
    uvicorn.run("polymarket_ai_agent.apps.api.main:app", host="127.0.0.1", port=8000, reload=False)
