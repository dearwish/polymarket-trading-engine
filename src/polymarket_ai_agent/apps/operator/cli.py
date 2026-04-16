from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from polymarket_ai_agent.config import get_settings
from polymarket_ai_agent.service import AgentService

app = typer.Typer(help="Operator CLI for the Polymarket AI agent.")
console = Console()


def _service() -> AgentService:
    return AgentService(get_settings())


@app.command()
def scan(limit: int = typer.Option(10, min=1, max=100)) -> None:
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


@app.command()
def analyze(market_id: str) -> None:
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


@app.command()
def paper(market_id: str) -> None:
    service = _service()
    snapshot, assessment, decision, result = service.paper_trade(market_id)
    console.print_json(
        json.dumps(
            {
                "market_id": market_id,
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


@app.command()
def status() -> None:
    service = _service()
    console.print_json(json.dumps(service.status()))


@app.command()
def report(session_id: str = "") -> None:
    service = _service()
    generated = service.generate_operator_report(session_id or None)
    table = Table(title=f"Report {generated.session_id}")
    table.add_column("Items")
    for item in generated.items:
        table.add_row(item)
    console.print(table)
