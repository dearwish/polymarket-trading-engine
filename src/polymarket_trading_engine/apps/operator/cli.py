from __future__ import annotations

import json
import time

import httpx
import typer
from rich.console import Console
from rich.table import Table

from polymarket_trading_engine.apps.daemon.run import run_daemon
from polymarket_trading_engine.config import get_settings
from polymarket_trading_engine.service import AgentService

app = typer.Typer(help="Operator CLI for the Polymarket Trading Engine.")
console = Console()


def _service() -> AgentService:
    return AgentService(get_settings())


def _handle_operator_error(exc: Exception) -> None:
    if isinstance(exc, httpx.HTTPError):
        console.print(f"Request failed: {exc}", style="red")
        raise typer.Exit(code=1) from exc
    if isinstance(exc, (OSError, RuntimeError, ValueError)):
        console.print(f"Operation failed: {exc}", style="red")
        raise typer.Exit(code=1) from exc
    raise exc


def _resolve_market_id(service: AgentService, market_id: str, active: bool) -> str:
    if market_id:
        return market_id
    if active:
        return service.get_active_market_id()
    raise ValueError("Provide a market_id or pass --active.")


@app.command()
def scan(limit: int = typer.Option(10, min=1, max=100)) -> None:
    try:
        service = _service()
        markets = service.discover_markets()[:limit]
        table = Table(title="Discovered Markets")
        table.add_column("Market ID")
        table.add_column("Question")
        table.add_column("Implied")
        table.add_column("Liquidity")
        for market in markets:
            table.add_row(
                market.market_id,
                market.question[:72],
                f"{market.implied_probability:.3f}",
                f"{market.liquidity_usd:.2f}",
            )
        console.print(table)
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def analyze(market_id: str) -> None:
    try:
        service = _service()
        snapshot, assessment = service.analyze_market(market_id)
        console.print_json(
            json.dumps(
                {
                    "market_id": market_id,
                    "question": snapshot.candidate.question,
                    "midpoint": snapshot.orderbook.midpoint,
                    "spread": snapshot.orderbook.spread,
                    "seconds_to_expiry": snapshot.seconds_to_expiry,
                    "fair_probability": assessment.fair_probability,
                    "confidence": assessment.confidence,
                    "edge": assessment.edge,
                    "suggested_side": assessment.suggested_side.value,
                    "reasons_for_trade": assessment.reasons_for_trade,
                    "reasons_to_abstain": assessment.reasons_to_abstain,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def paper(
    market_id: str = typer.Argument("", help="Explicit market id to trade."),
    active: bool = typer.Option(False, "--active"),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active)
        snapshot, assessment, decision, result = service.paper_trade(resolved_market_id)
        console.print_json(
            json.dumps(
                {
                    "market_id": resolved_market_id,
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
                    "execution": {
                        "success": result.success,
                        "status": result.status,
                        "detail": result.detail,
                        "order_id": result.order_id,
                    },
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def simulate(
    market_id: str = typer.Argument("", help="Explicit market id to simulate."),
    active: bool = typer.Option(False, "--active"),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active)
        snapshot, assessment, decision = service.simulate_market(resolved_market_id)
        console.print_json(
            json.dumps(
                {
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
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def status() -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.status()))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("auth-check")
def auth_check() -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.auth_status()))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def doctor(
    market_id: str = typer.Argument("", help="Explicit market id to inspect."),
    active: bool = typer.Option(False, "--active"),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active) if (market_id or active) else ""
        console.print_json(json.dumps(service.doctor(resolved_market_id or None)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-preflight")
def live_preflight(
    market_id: str = typer.Argument("", help="Explicit market id to inspect for live readiness."),
    active: bool = typer.Option(False, "--active"),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active) if (market_id or active) else ""
        console.print_json(json.dumps(service.live_preflight(resolved_market_id or None)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-orders")
def live_orders() -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.live_orders()))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-order")
def live_order(order_id: str) -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.live_order_status(order_id)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-cancel")
def live_cancel(
    order_id: str,
    confirm_cancel: bool = typer.Option(False, "--confirm-cancel"),
) -> None:
    try:
        if not confirm_cancel:
            raise ValueError("Refusing live cancellation without --confirm-cancel.")
        service = _service()
        console.print_json(json.dumps(service.cancel_live_order(order_id)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-trades")
def live_trades(
    market_id: str = typer.Option("", "--market-id"),
    limit: int = typer.Option(20, min=1, max=200),
) -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.live_trades(market_id or None, limit=limit)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-trade")
def live_trade(
    trade_id: str,
    market_id: str = typer.Option("", "--market-id"),
    limit: int = typer.Option(100, min=1, max=500),
) -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.live_trade_status(trade_id, market_id or None, limit=limit)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-activity")
def live_activity(
    market_id: str = typer.Option("", "--market-id"),
    active: bool = typer.Option(False, "--active"),
    trade_limit: int = typer.Option(20, "--trade-limit", min=1, max=200),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active) if (market_id or active) else ""
        console.print_json(json.dumps(service.live_activity(resolved_market_id or None, trade_limit=trade_limit)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("tracked-live-orders")
def tracked_live_orders(limit: int = typer.Option(50, min=1, max=500)) -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.tracked_live_orders(limit=limit)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("refresh-live-orders")
def refresh_live_orders(limit: int = typer.Option(50, min=1, max=500)) -> None:
    try:
        service = _service()
        console.print_json(json.dumps(service.refresh_live_order_tracking(limit=limit)))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-reconcile")
def live_reconcile(
    market_id: str = typer.Option("", "--market-id"),
    active: bool = typer.Option(False, "--active"),
    trade_limit: int = typer.Option(20, "--trade-limit", min=1, max=200),
    order_limit: int = typer.Option(50, "--order-limit", min=1, max=500),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active) if (market_id or active) else ""
        console.print_json(
            json.dumps(
                service.live_reconcile(
                    resolved_market_id or None,
                    trade_limit=trade_limit,
                    order_limit=order_limit,
                )
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("live-watch")
def live_watch(
    market_id: str = typer.Argument("", help="Explicit market id to monitor."),
    active: bool = typer.Option(False, "--active"),
    iterations: int = typer.Option(1, min=1),
    interval_seconds: int = typer.Option(0, min=0),
    trade_limit: int = typer.Option(20, "--trade-limit", min=1, max=200),
    order_limit: int = typer.Option(50, "--order-limit", min=1, max=500),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active)
        cycles = []
        previous = None
        for idx in range(iterations):
            cycle = service.live_reconcile(
                resolved_market_id,
                trade_limit=trade_limit,
                order_limit=order_limit,
            )
            fingerprint = {
                "blockers": cycle["preflight"].get("blockers", []),
                "tracked_summary": cycle["tracked_orders"].get("summary", {}),
                "recent_trade_count": cycle["recent_trades"].get("count", 0),
            }
            cycle["changed"] = fingerprint != previous
            previous = fingerprint
            cycles.append(cycle)
            if idx < iterations - 1 and interval_seconds > 0:
                time.sleep(interval_seconds)
        console.print_json(
            json.dumps(
                {
                    "readonly": True,
                    "market_id": resolved_market_id,
                    "iterations_requested": iterations,
                    "iterations_completed": len(cycles),
                    "cycles": cycles,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def live(
    market_id: str = typer.Argument("", help="Explicit market id to trade live."),
    active: bool = typer.Option(False, "--active"),
    confirm_live: bool = typer.Option(False, "--confirm-live"),
) -> None:
    try:
        if not confirm_live:
            raise ValueError("Refusing live execution without --confirm-live.")
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active)
        snapshot, assessment, decision, result = service.live_trade(resolved_market_id)
        console.print_json(
            json.dumps(
                {
                    "market_id": resolved_market_id,
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
                        "asset_id": decision.asset_id,
                        "rejected_by": decision.rejected_by,
                    },
                    "execution": {
                        "success": result.success,
                        "status": result.status,
                        "detail": result.detail,
                        "order_id": result.order_id,
                    },
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def manage() -> None:
    try:
        service = _service()
        actions = service.manage_open_positions()
        console.print_json(
            json.dumps(
                {
                    "actions": [
                        {"market_id": action.market_id, "action": action.action, "reason": action.reason}
                        for action in actions
                    ]
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def close(market_id: str, reason: str = "manual_close") -> None:
    try:
        service = _service()
        action = service.close_position(market_id, reason=reason)
        console.print_json(
            json.dumps(
                {
                    "market_id": action.market_id,
                    "action": action.action,
                    "reason": action.reason,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command()
def report(session_id: str = "") -> None:
    try:
        service = _service()
        generated = service.generate_operator_report(session_id or None)
        table = Table(title=f"Report {generated.session_id}")
        table.add_column("Items")
        for item in generated.items:
            table.add_row(item)
        console.print(table)
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("run-loop")
def run_loop(
    market_id: str = typer.Argument("", help="Explicit market id to trade."),
    active: bool = typer.Option(False, "--active"),
    iterations: int = typer.Option(1, min=1),
    interval_seconds: int = typer.Option(0, min=0),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active)
        cycles = []
        stop_reason = service.safety_stop_reason()
        for idx in range(iterations):
            if stop_reason:
                break
            cycles.append(service.run_cycle(resolved_market_id))
            stop_reason = service.safety_stop_reason()
            if idx < iterations - 1 and interval_seconds > 0:
                time.sleep(interval_seconds)
        console.print_json(
            json.dumps(
                {
                    "market_id": resolved_market_id,
                    "iterations_requested": iterations,
                    "iterations_completed": len(cycles),
                    "stopped_early": bool(stop_reason),
                    "stop_reason": stop_reason,
                    "cycles": cycles,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("daemon")
def daemon(
    duration_seconds: float = typer.Option(
        0.0,
        "--duration-seconds",
        min=0.0,
        help="If > 0, stop the daemon after this many seconds (useful for smoke tests).",
    ),
) -> None:
    """Run the event-driven market-data daemon (Phase 1: read-only feeds)."""
    try:
        # Migrations run explicitly before the service so (a) the applied
        # list surfaces as the migrations_applied journal event on first
        # boot and (b) get_effective_settings() below sees the freshly
        # seeded baseline. Constructing AgentService once keeps the
        # engines and self.settings coherent.
        from polymarket_trading_engine.config import get_effective_settings
        from polymarket_trading_engine.engine.migrations import MigrationRunner

        base = get_settings()
        applied = MigrationRunner(base.db_path).run()
        effective = get_effective_settings()
        service = AgentService(effective)
        # AgentService's own migration call is a no-op once the schema is
        # in place; carry the original applied list through so the daemon
        # journals it exactly once on fresh boots.
        if applied:
            service.migrations_applied = applied
        run_daemon(effective, service, duration_seconds=duration_seconds or None)
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("maintenance")
def maintenance(
    prune_days: int = typer.Option(
        -1,
        "--prune-days",
        help="Days of history to keep (order_attempts, closed positions, terminal live orders). "
        "Defaults to DAEMON_PRUNE_HISTORY_DAYS.",
    ),
    vacuum: bool = typer.Option(False, "--vacuum", help="Run full VACUUM (takes an exclusive lock)."),
) -> None:
    """Run retention + WAL checkpoint + optional VACUUM against the SQLite state."""
    try:
        service = _service()
        settings = get_settings()
        days = prune_days if prune_days >= 0 else settings.daemon_prune_history_days
        history_pruned = service.portfolio.prune_history(days) if days > 0 else {}
        events_pruned = service.journal.prune_events_jsonl(
            settings.events_jsonl_max_bytes,
            keep_tail_bytes=settings.events_jsonl_keep_tail_bytes,
        )
        wal = service.portfolio.wal_checkpoint()
        if vacuum:
            service.portfolio.vacuum()
            service.journal.vacuum()
        payload = {
            "history_pruned": history_pruned,
            "events_jsonl_pruned": bool(events_pruned),
            "wal_checkpoint": {"busy": wal[0], "log_pages": wal[1], "checkpointed_pages": wal[2]},
            "vacuum": bool(vacuum),
            "db_size_bytes": service.journal.db_size_bytes(),
            "events_jsonl_size_bytes": service.journal.events_jsonl_size_bytes(),
        }
        console.print_json(json.dumps(payload))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("backup")
def backup(
    destination: str = typer.Argument(..., help="Path to write the backup. A timestamp is appended if it is a directory."),
) -> None:
    """Snapshot the SQLite state file via VACUUM INTO."""
    try:
        service = _service()
        import time as _time
        from pathlib import Path as _Path

        dest = _Path(destination)
        if dest.is_dir() or destination.endswith("/"):
            stamp = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
            dest = dest / f"agent.db.{stamp}"
        result = service.portfolio.backup(dest)
        console.print_json(
            json.dumps(
                {
                    "destination": str(result),
                    "size_bytes": result.stat().st_size if result.exists() else 0,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("heartbeat")
def heartbeat() -> None:
    """Print the daemon's most recent heartbeat payload (if any)."""
    try:
        from polymarket_trading_engine.apps.daemon.heartbeat import HeartbeatReader

        settings = get_settings()
        reader = HeartbeatReader(settings.heartbeat_path)
        payload = reader.read()
        age = reader.age_seconds()
        console.print_json(json.dumps({"age_seconds": age, "heartbeat": payload}))
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("simulate-loop")
def simulate_loop(
    market_id: str = typer.Argument("", help="Explicit market id to simulate."),
    active: bool = typer.Option(False, "--active"),
    iterations: int = typer.Option(1, min=1),
    interval_seconds: int = typer.Option(0, min=0),
) -> None:
    try:
        service = _service()
        resolved_market_id = _resolve_market_id(service, market_id, active)
        cycles = []
        stop_reason = service.safety_stop_reason()
        for idx in range(iterations):
            if stop_reason:
                break
            cycles.append(service.run_simulation_cycle(resolved_market_id))
            stop_reason = service.safety_stop_reason()
            if idx < iterations - 1 and interval_seconds > 0:
                time.sleep(interval_seconds)
        console.print_json(
            json.dumps(
                {
                    "market_id": resolved_market_id,
                    "readonly": True,
                    "iterations_requested": iterations,
                    "iterations_completed": len(cycles),
                    "stopped_early": bool(stop_reason),
                    "stop_reason": stop_reason,
                    "cycles": cycles,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@app.command("mm-stats")
def mm_stats() -> None:
    """Print market-maker strategy stats: persisted reward income, in-memory
    pending accrual, time-in-band, currently-resting quotes.

    Persisted total comes from the ``reward_accruals`` SQL table — that's
    every quote that has ended (cancelled / filled / TTL-expired). Pending
    + per-quote rates come from the daemon heartbeat — that's the in-flight
    accrual on quotes that are still resting in the book. Sum them for the
    current strategy-level reward income.
    """
    try:
        from polymarket_trading_engine.apps.daemon.heartbeat import HeartbeatReader

        settings = get_settings()
        service = _service()
        persisted = service.portfolio.total_reward_accrued("market_maker")
        reader = HeartbeatReader(settings.heartbeat_path)
        payload = reader.read() or {}
        hb_age = reader.age_seconds()
        pending = float(payload.get("mm_reward_pending_usd", 0.0) or 0.0)
        in_band_s = float(payload.get("mm_reward_in_band_seconds", 0.0) or 0.0)
        out_band_s = float(payload.get("mm_reward_out_band_seconds", 0.0) or 0.0)
        # The pending_makers heartbeat list contains both follow-maker
        # (single-rest) and MM (two-sided) entries; filter on strategy_id
        # so we only show the MM legs here.
        all_pending = payload.get("pending_makers") or []
        mm_pending_quotes = [
            q for q in all_pending if q.get("strategy_id") == "market_maker"
        ]

        # Top-level summary as a Rich table.
        summary = Table(title="MM strategy reward summary", show_header=False)
        summary.add_column("metric")
        summary.add_column("value", justify="right")
        summary.add_row("Persisted (reward_accruals SUM)", f"${persisted:.4f}")
        summary.add_row("Pending in-memory (heartbeat)", f"${pending:.4f}")
        summary.add_row("Total reward income", f"${persisted + pending:.4f}")
        summary.add_row(
            "In-band time (across resting quotes)",
            f"{in_band_s:.0f}s ({in_band_s / 60:.1f}m)",
        )
        summary.add_row(
            "Out-of-band time",
            f"{out_band_s:.0f}s ({out_band_s / 60:.1f}m)",
        )
        if in_band_s + out_band_s > 0:
            in_band_pct = in_band_s / (in_band_s + out_band_s) * 100
            summary.add_row("In-band ratio", f"{in_band_pct:.1f}%")
        summary.add_row(
            "Heartbeat age",
            f"{hb_age:.1f}s" if hb_age is not None else "no heartbeat",
        )
        console.print(summary)

        # Per-quote table.
        if mm_pending_quotes:
            quotes = Table(title="MM resting quotes")
            quotes.add_column("market", overflow="fold")
            quotes.add_column("side")
            quotes.add_column("limit", justify="right")
            quotes.add_column("size $", justify="right")
            quotes.add_column("age", justify="right")
            quotes.add_column("ttl left", justify="right")
            for q in mm_pending_quotes:
                quotes.add_row(
                    str(q.get("market_id", ""))[:20],
                    str(q.get("side", "")),
                    f"{float(q.get('limit_price', 0.0)):.4f}",
                    f"{float(q.get('size_usd', 0.0)):.0f}",
                    f"{float(q.get('age_seconds', 0.0)):.0f}s",
                    f"{float(q.get('ttl_remaining_seconds', 0.0)):.0f}s",
                )
            console.print(quotes)
        else:
            console.print(
                "(no MM quotes currently resting — strategy may be disabled, "
                "abstaining, or the heartbeat is stale)",
                style="yellow",
            )
    except Exception as exc:
        _handle_operator_error(exc)


# ---------------------------------------------------------------------------
# settings — DB-backed runtime-override management
#
# Every ``set`` lands as an append-only row in ``settings_changes``; the
# running daemon's reload loop picks it up within ``daemon_settings_reload
# _interval_seconds`` (default 2 s) and the change flows to every engine.
# ---------------------------------------------------------------------------

settings_app = typer.Typer(help="Inspect and modify runtime settings stored in the DB.")
app.add_typer(settings_app, name="settings")


def _coerce_setting_value(raw: str) -> object:
    """Interpret ``raw`` as a JSON literal when possible (numbers, bools,
    ``null``, quoted strings), otherwise return the bare string.

    ``settings set min_edge 0.05`` → float 0.05.
    ``settings set paper_tp_ladder 0.30:0.5`` → string (not valid JSON).
    ``settings set live_trading_enabled true`` → boolean True.
    """
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


@settings_app.command("list")
def settings_list() -> None:
    """Print every editable field with its current effective value."""
    try:
        from polymarket_trading_engine.config import editable_values_snapshot

        service = _service()
        # editable_values_snapshot reads from ``self.settings`` which, at
        # service init, already layers the DB overrides over the env defaults.
        console.print_json(json.dumps({"values": editable_values_snapshot(service.settings)}))
    except Exception as exc:
        _handle_operator_error(exc)


@settings_app.command("get")
def settings_get(field: str = typer.Argument(..., help="Setting field name.")) -> None:
    """Print the current effective value of a single field."""
    try:
        service = _service()
        value = service.settings_store.current_overrides().get(field)
        if value is None:
            value = getattr(service.settings, field, None)
        console.print_json(json.dumps({"field": field, "value": value}))
    except Exception as exc:
        _handle_operator_error(exc)


@settings_app.command("set")
def settings_set(
    field: str = typer.Argument(..., help="Setting field name."),
    value: str = typer.Argument(..., help="New value (JSON-encoded: numbers, true/false, strings)."),
    reason: str = typer.Option("", "--reason", help="Free-text note stored alongside the change."),
) -> None:
    """Write a new override to ``settings_changes``.

    The daemon reload loop picks this up automatically; no restart needed
    unless the field is flagged ``requires_restart`` in
    ``EDITABLE_SETTINGS_METADATA``.
    """
    try:
        from polymarket_trading_engine.config import EDITABLE_SETTINGS_METADATA, save_runtime_overrides

        if field not in EDITABLE_SETTINGS_METADATA:
            console.print(f"[red]Unknown editable field: {field}[/red]")
            raise typer.Exit(code=1)
        service = _service()
        coerced = _coerce_setting_value(value)
        last_id_before = service.settings_store.get_max_id()
        save_runtime_overrides(service.settings, {field: coerced})
        new_ids = [r.id for r in service.settings_store.list_changes(since_id=last_id_before)]
        # Attach the reason to the just-inserted row(s). Cheap: one UPDATE.
        if reason and new_ids:
            import sqlite3

            with sqlite3.connect(service.settings.db_path) as conn:
                conn.executemany(
                    "UPDATE settings_changes SET reason = ? WHERE id = ?",
                    [(reason, rid) for rid in new_ids],
                )
                conn.commit()
        meta = EDITABLE_SETTINGS_METADATA[field]
        requires_restart = bool(meta.get("requires_restart"))
        console.print_json(
            json.dumps(
                {
                    "field": field,
                    "value": coerced,
                    "row_ids": new_ids,
                    "requires_restart": requires_restart,
                }
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)


@settings_app.command("history")
def settings_history(
    field: str = typer.Option("", "--field", help="Filter by field name."),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
) -> None:
    """Print the change history (most recent last)."""
    try:
        service = _service()
        rows = service.settings_store.list_timeline()
        if field:
            rows = [r for r in rows if r.field == field]
        rows = rows[-limit:]
        console.print_json(
            json.dumps(
                [
                    {
                        "id": r.id,
                        "changed_at": r.changed_at,
                        "field": r.field,
                        "before": r.value_before,
                        "after": r.value_after,
                        "source": r.source,
                        "reason": r.reason,
                    }
                    for r in rows
                ]
            )
        )
    except Exception as exc:
        _handle_operator_error(exc)
