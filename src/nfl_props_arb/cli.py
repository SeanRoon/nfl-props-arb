"""Command-line interface.

Mostly read-only. `scan`, `markets`, `odds-template`, `baselines-template` and
`snapshot` touch nothing but the public gateway and the local filesystem.

`autotrade` is the exception, and the only command in this package that can move
money. It defaults to `--dry-run` and requires `--live` to be said out loud. See
`autotrade.py` for the guards and `execution/` for the venue client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from .autotrade import DEFAULT_MAX_PER_MARKET, HALT_FILE, parse_kickoff
from .baselines import (
    AUTOTRADE_PATH,
    DEFAULT_AUTOTRADE,
    Baseline,
    apply_overrides,
    load,
    write_autotrade_template,
)
from .baselines import DEFAULT_PATH as BASELINES_PATH
from .baselines import write_template as write_baselines
from .execution.dryrun import DryRunOrderClient
from .ledger import DEFAULT_PATH as LEDGER_PATH
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


def _baselines(
    path: Path,
    overrides: list[str] | None,
    defaults: dict[PropType, Baseline] | None = None,
) -> dict[PropType, Baseline]:
    try:
        table = load(path, defaults)
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
    include_started: Annotated[
        bool,
        typer.Option(help="Include games that have already kicked off"),
    ] = True,
) -> None:
    """Find NO offers priced below the book's implied floor, after fees.

    Games already under way are included by default, which preserves the
    reporting this command has always done -- but note they are not tradeable
    signals: the event may already have happened. `--no-include-started` drops
    them, and `autotrade` always drops them regardless of this flag.
    """
    table = _baselines(baselines_file, set_odds) if odds_source == "baseline" else {}
    provider = _provider(odds_source, odds_file, table or None)
    thresholds = (
        {p: (min_edge if min_edge is not None else b.min_edge_pts) for p, b in table.items()}
        if table
        else ({} if min_edge is None else dict.fromkeys(PropType, min_edge))
    )
    try:
        result = run_scan(
            provider,
            days=days,
            slippage=slippage,
            min_edge_pts=thresholds,
            exclude_started=not include_started,
        )
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


@app.command("autotrade-template")
def autotrade_template(
    out: Annotated[Path, typer.Option(help="Where to write the TOML")] = AUTOTRADE_PATH,
) -> None:
    """Write an editable file of fee-adjusted buy limits for `autotrade`."""
    path = write_autotrade_template(out)
    console.print(f"Wrote [bold]{path}[/bold].")
    console.print(
        "[dim]These are LIVE LIMITS, not assumptions: autotrade pays up to "
        "max_buy all-in and does not re-subtract fees.[/dim]"
    )


@app.command()
def autotrade(
    live: Annotated[
        bool,
        typer.Option("--live/--dry-run", help="Actually place orders. Off by default."),
    ] = False,
    max_per_market: Annotated[
        float, typer.Option(help="Cap on all-in cost per market, across all runs")
    ] = DEFAULT_MAX_PER_MARKET,
    config: Annotated[
        Path, typer.Option(help="Fee-adjusted buy limits TOML")
    ] = AUTOTRADE_PATH,
    days: Annotated[int, typer.Option(help="Kickoff window, in days ahead")] = 8,
    kickoff_buffer: Annotated[
        int, typer.Option(help="Minutes before kickoff to stop trading a game")
    ] = 15,
    ledger_path: Annotated[
        Path, typer.Option("--ledger", help="Append-only order ledger")
    ] = LEDGER_PATH,
    halt_file: Annotated[Path, typer.Option(help="Kill switch: exists = do nothing")] = HALT_FILE,
    set_odds: Annotated[
        list[str] | None,
        typer.Option("--set", help="Override a limit, e.g. --set two_pt=0.67"),
    ] = None,
    snapshot_dir: Annotated[
        Path, typer.Option("--snapshot-dir", help="Where to record the run")
    ] = Path("data/scans"),
) -> None:
    """Buy every qualifying NO offer on games that have not started.

    Defaults to a dry run. Nothing is placed without `--live`.
    """
    from .autotrade import execute, fit_to_budget, is_halted, plan_orders
    from .ledger import load_exposure
    from .snapshot import write_snapshot

    if is_halted(halt_file):
        console.print(f"[yellow]HALTED[/yellow] -- {halt_file} exists. Nothing done.")
        return

    buffer = timedelta(minutes=kickoff_buffer)
    thresholds = _baselines(config, set_odds, defaults=DEFAULT_AUTOTRADE)
    provider = BaselineOdds(thresholds)

    try:
        result = run_scan(
            provider,
            days=days,
            min_edge_pts=None,
            exclude_started=True,
            kickoff_buffer=buffer,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    run_id = result.generated_at.isoformat()
    exposure = load_exposure(ledger_path)
    planned, skipped = plan_orders(
        result,
        thresholds,
        exposure=exposure,
        cap=max_per_market,
        now=result.generated_at,
        kickoff_buffer=buffer,
    )

    write_snapshot(result, snapshot_dir)
    _render_plan(planned, skipped, result, live=live, cap=max_per_market)

    if not planned:
        return

    # Dry and live runs take the same path, differing only in the client. That
    # way a dry run exercises the kickoff re-check and the ledger writes too,
    # rather than testing a shorter route than the one that spends money.
    if live:
        client = _order_client()
        budget = client.buying_power()
        planned, short = fit_to_budget(planned, budget)
        console.print(
            f"\nBuying power ${budget:,.2f}; {len(planned)} order(s) fit, "
            f"{len(short)} dropped for funds."
        )
        if not planned:
            return
    else:
        client = DryRunOrderClient()
    placed = execute(
        planned,
        client,
        run_id=run_id,
        ledger_path=ledger_path,
        kickoff_buffer=buffer,
    )
    console.print()
    failures = 0
    for order, outcome in placed:
        colour = "red" if outcome.status == "rejected" else "green"
        if outcome.status == "rejected":
            failures += 1
        console.print(
            f"  [{colour}]{outcome.status:<9}[/{colour}] {order.slug} "
            f"{order.shares:.2f} @ {order.price:.4f}  {outcome.message}"
        )
    if not live:
        console.print("\n[dim]Dry run. Nothing placed. Re-run with --live to submit.[/dim]")
        return
    if failures:
        # Non-zero so Task Scheduler surfaces the run as failed.
        raise typer.Exit(1)


def _order_client() -> Any:
    """The live venue client, or a clear failure explaining what is missing."""
    from .execution.client import LiveOrderClient
    from .execution.credentials import MissingCredentialsError, load

    try:
        return LiveOrderClient(load())
    except MissingCredentialsError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _render_plan(
    planned: list[Any],
    skipped: list[dict[str, Any]],
    result: Any,
    *,
    live: bool,
    cap: float,
) -> None:
    mode = "[red]LIVE[/red]" if live else "[cyan]DRY RUN[/cyan]"
    console.print(
        f"\n[bold]autotrade[/bold] {mode}  "
        f"{result.considered} markets considered, "
        f"{len(result.triggered)} qualifying, "
        f"[bold]{len(planned)}[/bold] orders planned  "
        f"[dim]cap ${cap:,.2f}/market[/dim]"
    )
    if planned:
        table = Table(show_lines=False, header_style="bold", box=None, pad_edge=False)
        for col in ("Game", "Prop", "Price", "Shares", "All-in", "Edge", "EV", "Kickoff"):
            table.add_column(col, justify="right" if col != "Game" else "left")
        for o in planned:
            kickoff = parse_kickoff(o.kickoff)
            table.add_row(
                o.game.split()[0],
                o.prop,
                f"{o.price:.4f}",
                f"{o.shares:,.2f}",
                f"${o.all_in_cost:,.2f}",
                f"{o.edge_pts:+.2f}",
                f"${o.expected_value:,.2f}",
                f"{kickoff:%m-%d %H:%M}" if kickoff else "?",
            )
        console.print(table)
        total = sum(o.all_in_cost for o in planned)
        ev = sum(o.expected_value for o in planned)
        console.print(f"  [bold]${total:,.2f}[/bold] committed, ${ev:,.2f} modelled EV")
    else:
        console.print("  [yellow]Nothing to buy.[/yellow]")

    if skipped:
        reasons: dict[str, int] = {}
        for s in skipped:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        console.print(f"  [dim]{len(skipped)} qualifying rows not traded ({summary})[/dim]")


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
