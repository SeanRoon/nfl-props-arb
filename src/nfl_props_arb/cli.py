"""Command-line interface.

Mostly read-only. `scan`, `markets`, `odds-template`, `baselines-template` and
`snapshot` touch nothing but the public gateway and the local filesystem.

`extremes-paper` streams authenticated market data but places nothing.

`autotrade`, `bids`, `bids-cancel`, `extremes-live` and `extremes-live-cancel` are
the exceptions: the only commands that can move money or change orders. Each
defaults to `--dry-run` and requires `--live` to be said out loud. See
`autotrade.py`, `bids.py` and `extremes_live.py` for the guards and `execution/`
for the venue client. `extremes-live-reconcile` reads orders but changes none.
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
from .ledger import write_fills_csv
from .odds.base import OddsProvider
from .odds.baseline import BaselineOdds
from .odds.manual import DEFAULT_PATH, ManualOdds, write_template
from .polymarket.client import PolymarketUS, discover
from .props import PropType
from .report import render, to_json
from .scan import listed_only, run_scan

app = typer.Typer(
    add_completion=False,
    help="Scan Polymarket US NFL game props for +EV NO trades anchored to FanDuel.",
)
console = Console()
FILLS_CSV = Path("data/fills.csv")


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
    result = listed_only(result)
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
    rows = write_fills_csv(FILLS_CSV, ledger_path)
    console.print(f"\n[dim]{rows} fill(s) to date in {FILLS_CSV}[/dim]")
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
def bids(
    live: Annotated[
        bool,
        typer.Option("--live/--dry-run", help="Actually place the bids. Off by default."),
    ] = False,
    shares: Annotated[float, typer.Option(help="Shares per bid")] = 50.0,
    prop: Annotated[
        list[str] | None,
        typer.Option("--prop", help="Only these props, e.g. --prop two_pt (repeatable)"),
    ] = None,
    price: Annotated[
        float | None,
        typer.Option(help="NO price to bid; defaults to each prop's max_buy, never above it"),
    ] = None,
    config: Annotated[
        Path, typer.Option(help="Fee-adjusted buy limits TOML")
    ] = AUTOTRADE_PATH,
    days: Annotated[int, typer.Option(help="Kickoff window, in days ahead")] = 8,
    kickoff_buffer: Annotated[
        int, typer.Option(help="Minutes before kickoff each bid expires")
    ] = 15,
    halt_file: Annotated[Path, typer.Option(help="Kill switch: exists = do nothing")] = HALT_FILE,
) -> None:
    """Rest a post-only NO bid at max_buy in every market the autotrader targets.

    Each bid expires at kickoff minus the buffer, enforced by the venue. Markets
    with an open order already, or an offer the autotrader can take, are skipped.
    Defaults to a dry run.
    """
    from .autotrade import is_halted
    from .bids import fit_to_budget as fit_bids
    from .bids import log as log_bid
    from .bids import plan_bids, record

    if is_halted(halt_file):
        console.print(f"[yellow]HALTED[/yellow] -- {halt_file} exists. Nothing done.")
        return
    try:
        only = {PropType(p.strip().lower()) for p in prop} if prop else None
    except ValueError as exc:
        raise typer.BadParameter(f"unknown prop: {exc}") from exc

    thresholds = _baselines(config, None, defaults=DEFAULT_AUTOTRADE)
    buffer = timedelta(minutes=kickoff_buffer)
    now = datetime.now(UTC)
    with PolymarketUS() as pm:
        props, _ = discover(
            pm, *_window(days), exclude_started=True, now=now, kickoff_buffer=buffer
        )

    # Open orders are read in a dry run too, so the preview matches what --live does.
    client = _order_client()
    open_slugs = {
        o["marketSlug"] for o in client.open_orders() if o.get("outcomeSide") == "OUTCOME_SIDE_NO"
    }
    planned, skipped = plan_bids(
        props, thresholds, shares=shares, now=now, kickoff_buffer=buffer,
        open_slugs=open_slugs, only=only, price=price,
    )
    budget = client.buying_power()
    planned, short = fit_bids(planned, budget)
    skipped += short

    mode = "[red]LIVE[/red]" if live else "[cyan]DRY RUN[/cyan]"
    console.print(
        f"\n[bold]bids[/bold] {mode}  {len(planned)} bid(s) of {shares:g} shares, "
        f"${sum(b.notional for b in planned):,.2f} if every bid filled; "
        f"${budget:,.2f} buying power caps what can actually fill"
    )
    table = Table()
    for col in ("Game", "Prop", "Team", "NO bid", "Shares", "Cost if filled", "Expires (UTC)"):
        table.add_column(col)
    for b in planned:
        table.add_row(
            b.game, b.prop, b.team or "", f"{b.no_price:.2f}", f"{b.shares:g}",
            f"${b.notional:,.2f}", b.expires_at.strftime("%m-%d %H:%M"),
        )
    console.print(table)
    if skipped:
        reasons: dict[str, int] = {}
        for s in skipped:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        console.print(f"  [dim]skipped: {summary}[/dim]")
    if not live:
        console.print("\n[dim]Dry run. Nothing placed. Re-run with --live to place.[/dim]")
        return

    failures = 0
    for b in planned:
        outcome = client.place_bid(b.slug, b.no_price, b.shares, b.expires_at)
        event = "placed" if outcome.status == "resting" else outcome.status
        log_bid(record(b, event, order_id=outcome.order_id, note=outcome.message))
        colour = "green" if event == "placed" else "red"
        if event != "placed":
            failures += 1
        console.print(
            f"  [{colour}]{event:<9}[/{colour}] {b.slug} {b.shares:g} @ {b.no_price:.2f}  "
            f"{outcome.order_id or ''} {outcome.message}"
        )
    if failures:
        raise typer.Exit(1)


@app.command("bids-cancel")
def bids_cancel(
    live: Annotated[
        bool,
        typer.Option("--live/--dry-run", help="Actually cancel. Off by default."),
    ] = False,
    all_orders: Annotated[
        bool,
        typer.Option("--all", help="Every open NO order on NFL props, including manual ones"),
    ] = False,
) -> None:
    """Cancel open bids placed by `bids` (or, with --all, every open NFL prop NO order)."""
    from .bids import placed_order_ids

    client = _order_client()
    mine = placed_order_ids()
    targets = [
        o for o in client.open_orders()
        if o.get("outcomeSide") == "OUTCOME_SIDE_NO"
        and (o["id"] in mine or (all_orders and o["marketSlug"].startswith("astatc-nfl-")))
    ]
    mode = "[red]LIVE[/red]" if live else "[cyan]DRY RUN[/cyan]"
    console.print(f"\n[bold]bids-cancel[/bold] {mode}  {len(targets)} open order(s)")
    for o in targets:
        no_price = 1.0 - float(o["price"]["value"])
        source = "bids" if o["id"] in mine else "manual/other"
        line = (
            f"{o['marketSlug']} NO {no_price:.2f} x {o['leavesQuantity']} "
            f"({o['id']}, {source})"
        )
        if not live:
            console.print(f"  would cancel {line}")
            continue
        ok, detail = client.cancel(o["id"], o["marketSlug"])
        colour = "green" if ok else "red"
        console.print(f"  [{colour}]{detail}[/{colour}] {line}")
    if not live and targets:
        console.print("\n[dim]Dry run. Nothing cancelled. Re-run with --live to cancel.[/dim]")


@app.command()
def fills(
    ledger_path: Annotated[
        Path, typer.Option("--ledger", help="Append-only order ledger")
    ] = LEDGER_PATH,
    csv_out: Annotated[
        Path, typer.Option("--csv", help="Also rewrite this CSV of every fill")
    ] = FILLS_CSV,
) -> None:
    """Every order the autotrader has had filled, with totals. Read-only."""
    from .ledger import fills as load_fills

    rows = load_fills(ledger_path)
    write_fills_csv(csv_out, ledger_path)
    if not rows:
        console.print("No fills yet.")
        return

    table = Table(title=f"Autotrader fills ({len(rows)})")
    for col in ("Filled (UTC)", "Game", "Prop"):
        table.add_column(col)
    for col in ("Shares", "Limit", "Fill", "Cost", "Pays if NO"):
        table.add_column(col, justify="right")
    table.add_column("Order")
    for f in rows:
        table.add_row(
            f.ts[:16].replace("T", " "),
            f.game,
            f.prop,
            f"{f.shares:g}",
            f"{f.limit_price:.4f}",
            "-" if f.fill_price is None else f"{f.fill_price:.4f}",
            f"${f.cost:,.2f}",
            f"${f.payout_if_no:,.2f}",
            (f.order_id or "") + ("" if f.status == "filled" else " (partial)"),
        )
    console.print(table)
    cost = sum(f.cost for f in rows)
    payout = sum(f.payout_if_no for f in rows)
    console.print(
        f"  {sum(f.shares for f in rows):g} shares, ${cost:,.2f} spent, "
        f"${payout:,.2f} if every NO wins (${payout - cost:,.2f} profit)"
    )
    console.print(f"  [dim]CSV: {csv_out}[/dim]")


@app.command("extremes-paper")
def extremes_paper(
    hours: Annotated[
        float, typer.Option(help="Watch games kicking off up to this many hours ahead")
    ] = 36.0,
    shares: Annotated[float, typer.Option(help="Virtual shares per 1-cent bid")] = 100.0,
    groups_per_conn: Annotated[
        int, typer.Option(help="100-market groups per connection (venue max 5)")
    ] = 5,
    max_hours: Annotated[float, typer.Option(help="Stop after this long regardless")] = 40.0,
    log_path: Annotated[Path, typer.Option("--log", help="Append-only paper log")] = Path(
        "data/extremes/paper.jsonl"
    ),
    halt_file: Annotated[Path, typer.Option(help="Kill switch: exists = stop")] = HALT_FILE,
) -> None:
    """PAPER: virtual 1-cent YES and NO bids on every NFL market, held through the games.

    Streams every trade and book snapshot and records which bids would have
    filled, under both the optimistic and queue-aware models. Places nothing.
    Runs until every market finishes, --max-hours pass, or the HALT file appears.
    """
    import asyncio

    from .autotrade import is_halted
    from .execution.credentials import MissingCredentialsError
    from .extremes_paper import run

    if is_halted(halt_file):
        console.print(f"[yellow]HALTED[/yellow] -- {halt_file} exists. Nothing done.")
        return
    console.print(f"[bold]extremes-paper[/bold] [cyan]PAPER[/cyan]  {shares:g} shares a side, "
                  f"games within {hours:g}h, log {log_path}")
    from .awake import keep_awake

    try:
        with keep_awake() as awake:
            console.print(f"keeping the PC awake: {'yes' if awake else 'NO (not Windows?)'}")
            tracker = asyncio.run(
                run(
                    hours=hours, shares=shares, groups_per_conn=groups_per_conn,
                    max_hours=max_hours, log_path=log_path, halted=lambda: is_halted(halt_file),
                    say=lambda s: console.print(s, markup=False, highlight=False),
                )
            )
    except MissingCredentialsError as exc:
        raise typer.BadParameter(f"the market stream is authenticated: {exc}") from exc
    s = tracker.summary()
    console.print(
        f"done: {s['joined']:g} bids, {s['touched']:g} touched, "
        f"{s['optimistic_fills']:g} optimistic / {s['queue_fills']:g} queue-aware fills"
    )


@app.command("extremes-settle")
def extremes_settle(
    log_path: Annotated[Path, typer.Option("--log", help="Paper log to settle")] = Path(
        "data/extremes/paper.jsonl"
    ),
) -> None:
    """Fetch settlements for every filled paper bid not yet settled. No report.

    Also settles markets filled (really, or in the shadow model) by the live
    trader's logs beside it -- one settlements file serves both. Writes to
    settlements.jsonl beside the log, so it is safe to run while either trader
    streams. Progress is saved as it goes; rerun to resume.
    """
    from .extremes import bids_from_log
    from .extremes_live import LiveTracker
    from .extremes_live_run import read_live_records
    from .extremes_paper import read_log_with_settlements, settle_slugs

    records = read_log_with_settlements(log_path)
    slugs = {b.slug for b in bids_from_log(records).values() if b.optimistic_filled > 0}
    for name in ("live.jsonl", "live-dryrun.jsonl"):
        live_log = log_path.with_name(name)
        if not live_log.exists():
            continue
        t = LiveTracker()
        t.replay(read_live_records(live_log))
        slugs |= {k[0] for k, o in t.orders.items() if o.cum_qty > 0}
        slugs |= {b.slug for b in t.shadows.values() if b.optimistic_filled > 0}
    if not slugs and not records:
        console.print(f"No paper or live log beside {log_path} yet.")
        return
    settle_slugs(slugs, records, log_path,
                 say=lambda s: console.print(s, markup=False, highlight=False))


@app.command("extremes-report")
def extremes_report(
    log_path: Annotated[Path, typer.Option("--log", help="Paper log to read")] = Path(
        "data/extremes/paper.jsonl"
    ),
    settle: Annotated[
        bool, typer.Option("--settle/--no-settle", help="Fetch settlements for filled markets")
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """P&L of the 1-cent extremes paper trader, by market type, side and fill model."""
    import json

    from .extremes import bids_from_log, filled_rows, report_rows, settlements_from_log
    from .extremes_paper import read_log_with_settlements, settle_filled

    records = read_log_with_settlements(log_path)
    if not records:
        console.print(f"No paper log at {log_path} yet.")
        return
    if settle:
        records += settle_filled(records, log_path)
    bids = bids_from_log(records)
    settlements = settlements_from_log(records)
    rows = report_rows(bids, settlements)
    fills_ = filled_rows(bids, settlements)
    gaps = [r for r in records if r.get("event") == "gap"]
    if as_json:
        print(json.dumps({"groups": rows, "fills": fills_, "gaps": gaps}, indent=1, default=str))
        return

    def money(x: float) -> str:
        return f"${x:,.2f}"

    def rate(x: float | None) -> str:
        return "-" if x is None else f"{x:.1%}"

    table = Table(title=f"1c extremes, paper ({len(bids)} virtual bids)  break-even hit rate 1.0%")
    for col in ("Type", "Side", "Cross", "Bids", "Touched"):
        table.add_column(col)
    for col in ("Opt fills", "Opt P&L", "Opt hit", "Queue fills", "Queue P&L", "Queue hit",
                "Unsettled"):
        table.add_column(col, justify="right")
    for r in sorted(rows, key=lambda r: -r["queue_pnl"]):
        table.add_row(
            r["market_type"], r["side"], "yes" if r["would_cross"] else "",
            str(r["bids"]), str(r["touched"]),
            str(r["optimistic_fills"]), money(r["optimistic_pnl"]), rate(r["optimistic_hit_rate"]),
            str(r["queue_fills"]), money(r["queue_pnl"]), rate(r["queue_hit_rate"]),
            str(r["optimistic_unsettled"]),
        )
    console.print(table)
    clean = [r for r in rows if not r["would_cross"]]
    console.print(
        f"  Total, excluding would-cross: optimistic "
        f"[bold]{money(sum(r['optimistic_pnl'] for r in clean))}[/bold], queue-aware "
        f"[bold]{money(sum(r['queue_pnl'] for r in clean))}[/bold] "
        f"on {money(sum(r['queue_cost'] for r in clean))} filled"
    )
    stats_only = sum(r["stats_only"] for r in rows)
    if gaps or stats_only:
        console.print(
            f"  [yellow]{len(gaps)} stream gap(s); {stats_only} bid(s) where the venue's "
            "session low/high reached our price with no trade seen[/yellow]"
        )

    if fills_:
        ft = Table(title="Filled bids, by queue-aware P&L")
        for col in ("Game", "Type", "Side", "Market", "Touched (UTC)"):
            ft.add_column(col)
        for col in ("Queue ahead", "Vol @ px", "Opt", "Queue", "Settle", "Opt P&L", "Queue P&L"):
            ft.add_column(col, justify="right")
        for f in fills_:
            ft.add_row(
                f["game"].split()[0], f["market_type"], f["side"], f["title"][:48],
                (f["first_touch"] or "")[:16].replace("T", " "),
                f"{f['queue_at_join']:,.0f}", f"{f['volume_at_price']:,.0f}",
                f"{f['optimistic_filled']:g}", f"{f['queue_filled']:g}",
                "-" if f["settlement"] is None else f"{f['settlement']:g}",
                "-" if f["optimistic_pnl"] is None else money(f["optimistic_pnl"]),
                "-" if f["queue_pnl"] is None else money(f["queue_pnl"]),
            )
        console.print(ft)


def _live_log(live: bool, log_path: Path | None) -> Path:
    from .extremes_live_run import DRY_LOG, LIVE_LOG

    return log_path or (LIVE_LOG if live else DRY_LOG)


@app.command("extremes-live")
def extremes_live(
    live: Annotated[
        bool, typer.Option("--live/--dry-run", help="Place real orders. Off by default.")
    ] = False,
    shares: Annotated[float, typer.Option(help="Shares per 1-cent bid")] = 1.0,
    skip: Annotated[
        list[str] | None,
        typer.Option("--skip", help="TYPE:SIDE slice not to bid (repeatable). "
                     "Default SPREAD:NO and TOTAL:NO; pass --skip NONE to bid everything."),
    ] = None,
    max_exposure: Annotated[
        float, typer.Option(help="Most dollars that can be lost on unfinished markets")
    ] = 200.0,
    hours: Annotated[float, typer.Option(help="Bid games kicking off up to this far ahead")] = 36.0,
    max_hours: Annotated[float, typer.Option(help="Stop after this long regardless")] = 20.0,
    log_path: Annotated[Path | None, typer.Option("--log", help="Override the log")] = None,
    game: Annotated[
        str | None, typer.Option(help="Only events whose ticker contains this, e.g. cin-was")
    ] = None,
    quota: Annotated[
        list[str] | None,
        typer.Option("--quota", help="TYPE:SIDE=N, the most bids in that slice across the "
                     "log (repeatable). Given any, slices without one get no bids."),
    ] = None,
    keys_from: Annotated[
        Path | None,
        typer.Option("--keys-from", help="Bid only the market sides an earlier live log "
                     "bid, to repeat a test on the same bids"),
    ] = None,
    halt_file: Annotated[Path, typer.Option(help="Kill switch: exists = stop")] = HALT_FILE,
) -> None:
    """1-cent YES/NO bids on every NFL market, each shadowed by the queue-aware model.

    Rests a post-only bid on each market side the paper trader would join
    (minus the --skip slices), held through the game, and runs the queue-aware
    model on exactly those orders from the moment each rests. The live report
    compares real fills with the shadow's. Dry run acks every bid on the spot
    and places nothing.
    """
    import asyncio

    from .autotrade import is_halted
    from .execution.credentials import MissingCredentialsError
    from .extremes_live import DEFAULT_SKIP, parse_quotas, parse_skip
    from .extremes_live_run import run

    if is_halted(halt_file):
        console.print(f"[yellow]HALTED[/yellow] -- {halt_file} exists. Nothing done.")
        return
    if skip is None:
        slices = DEFAULT_SKIP
    elif [s.upper() for s in skip] == ["NONE"]:
        slices = frozenset()
    else:
        try:
            slices = parse_skip(skip)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    try:
        quotas = parse_quotas(quota) if quota else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    allowed = None
    if keys_from is not None:
        from .extremes_live import intent_keys
        from .extremes_paper import read_log

        allowed = intent_keys(read_log(keys_from))
        if not allowed:
            raise typer.BadParameter(f"no bids in {keys_from}")
    log = _live_log(live, log_path)
    if keys_from is not None and keys_from.resolve() == log.resolve():
        raise typer.BadParameter("--keys-from must be another log: every key in this one "
                                 "is already spent and would never be placed again")
    mode = "[red]LIVE[/red]" if live else "[cyan]DRY RUN[/cyan]"
    skipped = ", ".join(f"{t}:{s}" for t, s in sorted(slices)) or "none"
    console.print(f"[bold]extremes-live[/bold] {mode}  {shares:g} share(s) a side, "
                  f"skip {skipped}, cap ${max_exposure:,.2f}, "
                  f"{'game ' + game + ', ' if game else ''}"
                  f"{'quotas ' + ' '.join(quota) + ', ' if quota else ''}"
                  f"{f'{len(allowed)} keys from {keys_from}, ' if allowed else ''}log {log}")
    from .awake import keep_awake

    try:
        with keep_awake() as awake:
            console.print(f"keeping the PC awake: {'yes' if awake else 'NO (not Windows?)'}")
            tracker = asyncio.run(run(
                live=live, hours=hours, shares=shares, skip=slices, max_exposure=max_exposure,
                max_hours=max_hours, log_path=log, game=game, quotas=quotas,
                allowed_keys=allowed, halted=lambda: is_halted(halt_file),
                say=lambda s: console.print(s, markup=False, highlight=False),
            ))
    except MissingCredentialsError as exc:
        raise typer.BadParameter(f"the streams are authenticated: {exc}") from exc
    s = tracker.summary()
    console.print(f"done: {s['placed']:g} placed, {s['rejected']:g} rejected, "
                  f"{s['unknown']:g} unknown; fills real {s['real_fills']:g} / "
                  f"shadow {s['shadow_fills']:g}")


@app.command("extremes-live-report")
def extremes_live_report(
    live: Annotated[bool, typer.Option("--live/--dry-run", help="Which log to read")] = True,
    log_path: Annotated[Path | None, typer.Option("--log", help="Override the log")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Real fills and P&L against the queue-aware shadow, bid for bid. Reads cached
    settlements only; `extremes-settle` fetches them."""
    import json
    from collections import Counter

    from .extremes import settlements_from_log
    from .extremes_live import LiveTracker, compare_groups, compare_rows
    from .extremes_live_run import read_live_records
    from .extremes_paper import read_log, settlements_path

    log = _live_log(live, log_path)
    records = read_live_records(log)
    if not records:
        console.print(f"No live log at {log} yet.")
        return
    tracker = LiveTracker()
    tracker.replay(records)
    settlements = settlements_from_log(records + read_log(settlements_path(log)))
    rows = compare_rows(tracker, settlements)
    groups = compare_groups(rows)
    skips = Counter(tracker.skipped.values())
    statuses = Counter(o.status for o in tracker.orders.values())
    if as_json:
        print(json.dumps({"groups": groups, "orders": rows, "skips": skips,
                          "statuses": statuses}, indent=1, default=str))
        return

    def money(x: float) -> str:
        return f"${x:,.2f}"

    table = Table(title=f"1c extremes, real vs queue-aware shadow ({log.name})")
    for col in ("Type", "Side"):
        table.add_column(col)
    for col in ("Orders", "Real fills", "Shadow fills", "Both", "Real only", "Shadow only",
                "Real P&L", "Shadow P&L", "Unsettled"):
        table.add_column(col, justify="right")
    for g in groups:
        table.add_row(g["market_type"], g["side"], str(g["orders"]), str(g["real_fills"]),
                      str(g["shadow_fills"]), str(g["both"]), str(g["real_only"]),
                      str(g["shadow_only"]), money(g["real_pnl"]), money(g["shadow_pnl"]),
                      str(g["unsettled"]))
    console.print(table)
    agree = sum(r["agree"] for r in rows)
    console.print(
        f"  Real [bold]{money(sum(g['real_pnl'] for g in groups))}[/bold] vs shadow "
        f"[bold]{money(sum(g['shadow_pnl'] for g in groups))}[/bold] (settled only). "
        f"Fill/no-fill agreement on {agree}/{len(rows)} orders."
    )
    console.print(f"  Orders: {dict(statuses)}; skipped: {dict(skips)}; "
                  f"{sum(not r['shadowed'] for r in rows)} rested without a shadow join, "
                  f"{sum(r['stopped'] for r in rows)} cancelled/expired by the venue")
    gaps = [r for r in records if r.get("event") == "gap"]
    if gaps:
        console.print(f"  [yellow]{len(gaps)} stream gap(s); run extremes-live-reconcile "
                      "for real fills missed meanwhile[/yellow]")
    diff = [r for r in rows if not r["agree"]
            or abs(r["real_filled"] - r["shadow_queue_filled"]) > 1e-9]
    if diff:
        dt = Table(title="Where real and shadow disagree")
        for col in ("Type", "Side", "Market", "Queue at join", "Real", "Shadow", "Settle"):
            dt.add_column(col)
        for r in diff[:50]:
            dt.add_row(r["market_type"], r["side"], (r["title"] or r["slug"])[:48],
                       f"{r['queue_at_join']:,.0f}" if r["queue_at_join"] is not None else "-",
                       f"{r['real_filled']:g}", f"{r['shadow_queue_filled']:g}",
                       "-" if r["settlement"] is None else f"{r['settlement']:g}")
        console.print(dt)


@app.command("extremes-live-reconcile")
def extremes_live_reconcile(
    log_path: Annotated[Path | None, typer.Option("--log", help="Override the log")] = None,
) -> None:
    """Fetch the venue's state of every live order not known to be finished.

    Recovers fills and cancels the private stream missed. Read-only at the venue;
    writes live-reconcile.jsonl, so it is safe while the trader runs.
    """
    from .extremes_live_run import reconcile

    log = _live_log(True, log_path)
    with _order_client() as client:
        reconcile(log, client, say=lambda s: console.print(s, markup=False, highlight=False))


@app.command("extremes-live-cancel")
def extremes_live_cancel(
    live: Annotated[
        bool, typer.Option("--live/--dry-run", help="Actually cancel. Off by default.")
    ] = False,
    log_path: Annotated[Path | None, typer.Option("--log", help="Override the log")] = None,
) -> None:
    """Cancel every open order the live extremes trader placed. Nothing else is touched."""
    import time

    from .extremes_live import LiveTracker
    from .extremes_live_run import read_live_records

    log = _live_log(True, log_path)
    tracker = LiveTracker()
    tracker.replay(read_live_records(log))
    mine = set(tracker.by_id)
    with _order_client() as client:
        targets = [o for o in client.open_orders() if str(o.get("id")) in mine]
        console.print(f"{len(targets)} open order(s) placed by extremes-live "
                      f"({'[red]LIVE[/red]' if live else '[cyan]DRY RUN[/cyan]'})")
        if not live:
            return
        ok = 0
        for o in targets:
            done, detail = client.cancel(str(o["id"]), str(o.get("marketSlug") or ""))
            ok += done
            if not done:
                console.print(f"  {o['id']}: {detail}")
            time.sleep(0.1)
        console.print(f"cancelled {ok}/{len(targets)}")


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
