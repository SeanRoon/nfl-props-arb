"""Command-line interface.

Read-only by design: this package contains no order-placement code. Execution,
if it ever ships, belongs in a separate private repo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .odds.base import OddsProvider
from .odds.manual import DEFAULT_PATH, ManualOdds, write_template
from .polymarket.client import PolymarketUS, discover
from .report import render, to_json
from .scan import run_scan

app = typer.Typer(
    add_completion=False,
    help="Scan Polymarket US NFL game props for +EV NO trades anchored to FanDuel.",
)
console = Console()


def _window(days: int) -> tuple[str, str]:
    now = datetime.now(UTC)
    return (
        now.strftime("%Y-%m-%dT00:00:00Z"),
        (now + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z"),
    )


def _provider(source: str, odds_file: Path) -> OddsProvider:
    if source == "manual":
        return ManualOdds(odds_file)
    if source == "fanduel":
        from .odds.fanduel import FanDuelOdds

        return FanDuelOdds()
    raise typer.BadParameter(f"unknown odds source: {source}")


@app.command()
def scan(
    odds_source: Annotated[str, typer.Option(help="manual | fanduel")] = "fanduel",
    odds_file: Annotated[Path, typer.Option(help="CSV for --odds-source manual")] = DEFAULT_PATH,
    days: Annotated[int, typer.Option(help="Kickoff window, in days ahead")] = 8,
    slippage: Annotated[
        float, typer.Option(help="Extra cushion beyond the modelled taker fee")
    ] = 0.0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table")] = False,
    ladder: Annotated[bool, typer.Option(help="Show the per-level liquidity ladder")] = True,
) -> None:
    """Find NO offers priced below FanDuel's implied floor, after fees."""
    provider = _provider(odds_source, odds_file)
    try:
        result = run_scan(provider, days=days, slippage=slippage)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if as_json:
        print(to_json(result))
    else:
        render(result, console, show_ladder=ladder)


@app.command()
def markets(
    days: Annotated[int, typer.Option(help="Kickoff window, in days ahead")] = 8,
    books: Annotated[bool, typer.Option(help="Fetch each book for live NO asks")] = False,
) -> None:
    """List the Polymarket US props this tool knows how to price."""
    lo, hi = _window(days)
    with PolymarketUS() as client:
        props, skipped = discover(client, lo, hi)
        if books:
            for p in props:
                client.load_book(p)
    table = Table(header_style="bold")
    for col in ("Game", "Prop", "Scope", "NO ask", "Theta", "Slug"):
        table.add_column(col)
    for p in sorted(props, key=lambda x: (x.key.game, x.key.prop.value, x.key.team or "")):
        table.add_row(
            str(p.key.game),
            p.key.prop.value,
            p.key.team.upper() if p.key.team else "game",
            f"{p.no_ask:.4f}" if p.no_ask is not None else "-",
            f"{p.theta:g}",
            p.slug,
        )
    console.print(table)
    console.print(f"[dim]{len(props)} markets, {len(skipped)} skipped[/dim]")


@app.command("odds-template")
def odds_template(
    days: Annotated[int, typer.Option(help="Kickoff window, in days ahead")] = 8,
    out: Annotated[Path, typer.Option(help="Where to write the CSV")] = DEFAULT_PATH,
) -> None:
    """Emit a CSV of every prop awaiting a hand-entered FanDuel price."""
    lo, hi = _window(days)
    with PolymarketUS() as client:
        props, _ = discover(client, lo, hi)
    games = sorted({p.key.game for p in props})
    path = write_template(games, out)
    console.print(f"Wrote [bold]{path}[/bold] -- {len(games)} games.")
    console.print(
        "[dim]Fill the american_odds column, then: "
        "nflprops scan --odds-source manual[/dim]"
    )


@app.command()
def snapshot(
    odds_source: Annotated[str, typer.Option(help="manual | fanduel")] = "fanduel",
    odds_file: Annotated[Path, typer.Option()] = DEFAULT_PATH,
    days: Annotated[int, typer.Option()] = 8,
    out_dir: Annotated[Path, typer.Option()] = Path("data/scans"),
) -> None:
    """Record the current slate to Parquet, building price history over time."""
    from .snapshot import write_snapshot

    result = run_scan(_provider(odds_source, odds_file), days=days)
    path = write_snapshot(result, out_dir)
    console.print(f"Wrote [bold]{path}[/bold] ({result.considered} markets)")


if __name__ == "__main__":
    app()
