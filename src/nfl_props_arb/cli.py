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

from .baselines import DEFAULT_PATH as BASELINES_PATH
from .baselines import Baseline, apply_overrides, load
from .baselines import write_template as write_baselines
from .odds.base import OddsProvider
from .odds.baseline import BaselineOdds
from .odds.manual import DEFAULT_PATH, ManualOdds, write_template
from .polymarket.client import PolymarketUS, discover
from .props import PropType
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


def _baselines(path: Path, overrides: list[str] | None) -> dict[PropType, Baseline]:
    try:
        table = load(path)
        return apply_overrides(table, overrides or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _provider(
    source: str,
    odds_file: Path,
    baselines: dict[PropType, Baseline] | None = None,
) -> OddsProvider:
    if source == "baseline":
        return BaselineOdds(baselines)
    if source == "manual":
        return ManualOdds(odds_file)
    if source == "fanduel":
        from .odds.fanduel import FanDuelOdds

        return FanDuelOdds()
    raise typer.BadParameter(f"unknown odds source: {source}")


@app.command()
def scan(
    odds_source: Annotated[str, typer.Option(help="baseline | manual | fanduel")] = "baseline",
    odds_file: Annotated[Path, typer.Option(help="CSV for --odds-source manual")] = DEFAULT_PATH,
    days: Annotated[int, typer.Option(help="Kickoff window, in days ahead")] = 8,
    slippage: Annotated[
        float, typer.Option(help="Extra cushion beyond the modelled taker fee")
    ] = 0.0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table")] = False,
    ladder: Annotated[bool, typer.Option(help="Show the per-level liquidity ladder")] = True,
    baselines_file: Annotated[
        Path, typer.Option("--baselines", help="Baseline odds TOML")
    ] = BASELINES_PATH,
    set_odds: Annotated[
        list[str] | None,
        typer.Option("--set", help="Override a baseline, e.g. --set dst_td=+850"),
    ] = None,
    min_edge: Annotated[
        float | None,
        typer.Option(help="Global minimum edge in points; overrides per-prop values"),
    ] = None,
) -> None:
    """Find NO offers priced below the book's implied floor, after fees."""
    table = _baselines(baselines_file, set_odds) if odds_source == "baseline" else {}
    provider = _provider(odds_source, odds_file, table or None)
    thresholds = (
        {p: (min_edge if min_edge is not None else b.min_edge_pts) for p, b in table.items()}
        if table
        else ({} if min_edge is None else dict.fromkeys(PropType, min_edge))
    )
    try:
        result = run_scan(provider, days=days, slippage=slippage, min_edge_pts=thresholds)
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


@app.command("baselines-template")
def baselines_template(
    out: Annotated[Path, typer.Option(help="Where to write the TOML")] = BASELINES_PATH,
) -> None:
    """Write an editable baseline-odds file with the built-in defaults."""
    path = write_baselines(out)
    console.print(f"Wrote [bold]{path}[/bold].")
    console.print(
        "[dim]Baselines are assumptions. The scan prints the price each market "
        "would need, so verify the top edges against a book.[/dim]"
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
