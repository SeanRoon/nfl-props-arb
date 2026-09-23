"""Rendering scan results: rich tables, JSON, and markdown."""

from __future__ import annotations

import json
from typing import Any, Literal

from rich.console import Console
from rich.table import Table

from .edge import required_american_odds
from .odds.base import PropQuote
from .scan import Opportunity, ScanResult


def _sign(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _book_cell(q: PropQuote) -> str:
    """How a quote reads in the Book column.

    A fee-adjusted operator limit is not a book price and must not be dressed up
    as one, so it shows as the cap it is.
    """
    if q.american is None:
        return f"<={q.max_buy:.3f}" if q.max_buy is not None else "-"
    return _sign(q.american)


def opportunity_dict(o: Opportunity) -> dict[str, Any]:
    """Serialisable view. Includes the raw strings needed to verify a match."""
    best = o.best
    return {
        "game": str(o.prop.key.game),
        "prop": o.prop.key.prop.value,
        "scope": o.prop.key.scope.value,
        "team": o.prop.key.team,
        "kickoff": o.prop.start_date,
        "polymarket": {
            "slug": o.prop.slug,
            "question": o.prop.question,
            "title": o.prop.title,
            "theta": o.prop.theta,
            "no_ask": o.prop.no_ask,
            "url": o.prop.url,
        },
        "fanduel": [
            {
                "label": q.label,
                "american": q.american,
                "max_buy": q.max_buy,
                "implied_yes": q.implied_yes,
                "scope": q.scope.value,
                "team": q.team,
                "source": q.source,
            }
            for q in o.quotes
        ],
        "floor": {
            "value": o.floor.value,
            "method": o.floor.method,
            "explanation": o.floor.explanation,
        },
        "max_buy_price": o.max_buy,
        "required_american_odds": (
            required_american_odds(best.no_price, o.prop.theta) if best else None
        ),
        "floor_is_assumed": any(q.source == "baseline" for q in o.quotes),
        "triggered": o.triggered,
        "best_edge_pts": best.edge_pts if best else None,
        "roi": best.roi if best else None,
        "total_shares": o.total_shares,
        "total_cost": o.total_cost,
        "total_ev": o.total_ev,
        "ladder": [
            {
                "no_price": e.no_price,
                "qty": e.qty,
                "fee_per_share": e.fee,
                "all_in": e.all_in,
                "edge_pts": e.edge_pts,
                "roi": e.roi,
            }
            for e in o.edges
        ],
    }


def to_json(result: ScanResult) -> str:
    return json.dumps(
        {
            "generated_at": result.generated_at.isoformat(),
            "considered": result.considered,
            "triggered": len(result.triggered),
            "opportunities": [opportunity_dict(o) for o in result.triggered],
            "unpriced": result.unpriced,
        },
        indent=2,
    )


def render(result: ScanResult, console: Console, *, show_ladder: bool = True) -> None:
    """Print the operator-facing report."""
    hits = result.triggered
    console.print(
        f"\n[bold]NFL game props[/bold]  "
        f"{result.considered} markets considered, "
        f"[bold]{len(hits)}[/bold] with +EV NO liquidity  "
        f"[dim]{result.generated_at:%Y-%m-%d %H:%M UTC}[/dim]"
    )

    if not hits:
        console.print(
            "\n[yellow]No opportunities.[/yellow] "
            "Every NO offer priced above the fee-adjusted FanDuel floor."
        )
        _print_unpriced(result, console)
        return

    table = Table(show_lines=False, header_style="bold", box=None, pad_edge=False)
    columns: tuple[tuple[str, Literal["left", "right"]], ...] = (
        ("Game", "left"), ("Prop", "left"), ("Book", "right"),
        ("Floor", "right"), ("Max", "right"), ("NO", "right"),
        ("Edge", "right"), ("ROI", "right"), ("Shares", "right"),
        ("Cost", "right"), ("EV", "right"), ("Book need", "right"),
    )
    for col, just in columns:
        table.add_column(col, justify=just, overflow="fold")

    for o in hits:
        best = o.best
        assert best is not None
        key = o.prop.key
        matchup = f"{key.game.away.upper()}@{key.game.home.upper()}"
        prop = key.prop.value if not key.team else f"{key.prop.value}:{key.team.upper()}"
        table.add_row(
            matchup,
            prop,
            "/".join(_book_cell(q) for q in o.quotes) or "-",
            f"{o.floor.value:.3f}",
            f"{o.max_buy:.3f}",
            f"{best.no_price:.3f}",
            f"[green]{best.edge_pts:+.1f}[/green]",
            f"{best.roi:.1%}",
            f"{o.total_shares:,.2f}",
            f"${o.total_cost:,.2f}",
            f"${o.total_ev:,.2f}",
            (lambda a: f"{a:+d} or longer" if a is not None else "-")(
                required_american_odds(best.no_price, o.prop.theta)
            ),
        )
    console.print(table)

    if show_ladder:
        for o in hits:
            console.print(f"\n[bold]{o.prop.key}[/bold]  [dim]{o.prop.slug}[/dim]")
            console.print(f"  PM : [cyan]{o.prop.question}[/cyan]")
            console.print(f"  FD : {o.fd_summary}")
            console.print(
                f"  floor {o.floor.value:.4f} via {o.floor.method} "
                f"[dim]({o.floor.explanation})[/dim]  ->  max buy {o.max_buy:.4f} "
                f"[dim](theta={o.prop.theta})[/dim]"
            )
            for e in o.edges:
                console.print(
                    f"    NO {e.no_price:.4f} x {e.qty:>10,.2f} sh   "
                    f"fee {e.fee:.4f}  all-in {e.all_in:.4f}  "
                    f"edge [green]{e.edge_pts:+.2f}[/green] pts  "
                    f"EV ${e.ev_per_share * e.qty:,.2f}"
                )
    _print_unpriced(result, console)


def _print_unpriced(result: ScanResult, console: Console) -> None:
    if not result.unpriced:
        return
    reasons: dict[str, int] = {}
    for u in result.unpriced:
        key = (u.get("reason") or "unknown").split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
    console.print(f"\n[dim]{len(result.unpriced)} markets not priced ({summary})[/dim]")
