"""Focused tests for the per-strategy breakdown in scripts/analyze_soak.py.

The script is primarily a CLI tool so most of it is I/O; these tests cover
the pure helper that future phases rely on to compare strategies offline
without spinning up the daemon.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_analyze_soak():
    """Import the script as a module. It lives outside the package
    directory, so the canonical import path isn't available — this helper
    keeps the test self-contained and stable against future package moves.

    Registers in sys.modules before executing so dataclasses resolving
    annotations via cls.__module__ can find it.
    """
    if "analyze_soak" in sys.modules:
        return sys.modules["analyze_soak"]
    script = Path(__file__).resolve().parent.parent / "scripts" / "analyze_soak.py"
    spec = importlib.util.spec_from_file_location("analyze_soak", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyze_soak"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_strategy_breakdown_skipped_for_single_strategy(capsys) -> None:
    """With only one strategy emitting closes, the breakdown section is
    skipped — the numbers would just duplicate the aggregate view.
    """
    mod = _load_analyze_soak()
    ClosedPosition = mod.ClosedPositionRecord
    rows = [
        ClosedPosition(
            market_id=f"m-{i}",
            side="YES",
            size_usd=10.0,
            entry_price=0.5,
            exit_price=0.6,
            realized_pnl=2.0,
            close_reason="paper_take_profit",
            hold_seconds=120.0,
            strategy_id="fade",
        )
        for i in range(3)
    ]
    mod._print_strategy_breakdown(rows)
    out = capsys.readouterr().out
    assert "Per-strategy breakdown" not in out


def test_strategy_breakdown_prints_per_strategy_rows(capsys) -> None:
    """Two strategies → breakdown table shows one row each, with the
    correct aggregated PnL and win rate per strategy_id.
    """
    mod = _load_analyze_soak()
    ClosedPosition = mod.ClosedPositionRecord
    rows = [
        ClosedPosition(
            market_id="m-1", side="YES", size_usd=10.0, entry_price=0.5,
            exit_price=0.6, realized_pnl=2.0, close_reason="paper_take_profit",
            hold_seconds=120.0, strategy_id="fade",
        ),
        ClosedPosition(
            market_id="m-2", side="NO", size_usd=10.0, entry_price=0.5,
            exit_price=0.4, realized_pnl=-2.0, close_reason="paper_stop_loss",
            hold_seconds=60.0, strategy_id="fade",
        ),
        ClosedPosition(
            market_id="m-1", side="NO", size_usd=10.0, entry_price=0.5,
            exit_price=0.6, realized_pnl=2.0, close_reason="paper_take_profit",
            hold_seconds=300.0, strategy_id="adaptive",
        ),
    ]
    mod._print_strategy_breakdown(rows)
    out = capsys.readouterr().out
    assert "Per-strategy breakdown" in out
    # Per-strategy aggregate rows
    assert "fade" in out
    assert "adaptive" in out
    # fade: 2 trades, net 0.00, 1 win → 50%
    # adaptive: 1 trade, +2.00, 1 win → 100%
    assert "50.0%" in out
    assert "100.0%" in out


def test_load_ticks_filters_by_strategy(tmp_path) -> None:
    """``--strategy`` must partition the tick stream so per-strategy
    aggregate stats don't blend fade and adaptive.
    """
    import json
    mod = _load_analyze_soak()
    events = tmp_path / "events.jsonl"

    def _tick(strategy_id: str, market: str = "m-1") -> dict:
        return {
            "event_type": "daemon_tick",
            "logged_at": "2026-04-22T18:00:00+00:00",
            "payload": {
                "market_id": market,
                "question": "q",
                "strategy_id": strategy_id,
                "suggested_side": "YES",
                "fair_probability": 0.6,
                "edge_yes": 0.05,
                "edge_no": -0.05,
                "confidence": 0.7,
                "bid_yes": 0.58,
                "ask_yes": 0.60,
                "btc_price": 70000.0,
                "btc_session": "us",
                "btc_log_return_1h": 0.0,
            },
        }

    events.write_text(
        "\n".join(
            json.dumps(t) for t in [
                _tick("fade"),
                _tick("adaptive"),
                _tick("fade", "m-2"),
                _tick("adaptive", "m-2"),
            ]
        )
    )
    all_summaries = mod.load_ticks(events)
    assert sum(len(ms.ticks) for ms in all_summaries.values()) == 4

    fade_only = mod.load_ticks(events, strategy_id="fade")
    assert sum(len(ms.ticks) for ms in fade_only.values()) == 2
    assert all(
        t.strategy_id == "fade"
        for ms in fade_only.values() for t in ms.ticks
    )

    adaptive_only = mod.load_ticks(events, strategy_id="adaptive")
    assert sum(len(ms.ticks) for ms in adaptive_only.values()) == 2
    assert all(
        t.strategy_id == "adaptive"
        for ms in adaptive_only.values() for t in ms.ticks
    )


def test_load_ticks_defaults_missing_strategy_to_fade(tmp_path) -> None:
    """Pre-phase-1 events.jsonl entries don't carry strategy_id. The
    loader must tag them 'fade' so legacy soak data stays analyzable
    and shows up under the fade strategy without a migration.
    """
    import json
    mod = _load_analyze_soak()
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({
        "event_type": "daemon_tick",
        "logged_at": "2026-04-22T18:00:00+00:00",
        "payload": {
            "market_id": "legacy",
            "question": "q",
            # no strategy_id — legacy tick
            "suggested_side": "YES",
            "fair_probability": 0.6,
            "edge_yes": 0.05,
            "edge_no": -0.05,
            "confidence": 0.7,
            "bid_yes": 0.58,
            "ask_yes": 0.60,
            "btc_price": 70000.0,
            "btc_session": "us",
            "btc_log_return_1h": 0.0,
        },
    }))
    summaries = mod.load_ticks(events, strategy_id="fade")
    assert sum(len(ms.ticks) for ms in summaries.values()) == 1
