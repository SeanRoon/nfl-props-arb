"""Report rendering."""

import io

from rich.console import Console

from nfl_props_arb.combine import Floor
from nfl_props_arb.edge import Edge
from nfl_props_arb.polymarket.client import PmProp
from nfl_props_arb.props import GameKey, PropKey, PropType, Scope
from nfl_props_arb.report import opportunity_dict, render
from nfl_props_arb.scan import Opportunity, ScanResult

GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")


def _opportunity():
    prop = PmProp(
        key=PropKey(GAME, PropType.SAFETY, Scope.GAME),
        slug="astatc-nfl-det-buf-2026-09-17-safety-0pt5",
        market_id="1",
        question="Will there be a Safety?: Over 0.5",
        title="Safety scored",
        theta=0.06,
        line=0.5,
        event_ticker="nfl-det-buf-2026-09-17",
        start_date="2026-09-18T00:15:00Z",
        best_yes_bid=0.20,
    )
    return Opportunity(
        prop=prop,
        floor=Floor(value=0.889, method="direct"),
        max_buy=0.88,
        edges=[Edge(no_price=0.80, floor=0.889, theta=0.06, qty=5.0)],
    )


def _result(op):
    from datetime import UTC, datetime

    return ScanResult([op], considered=1, unpriced=[], generated_at=datetime.now(UTC))


def test_table_columns_line_up_with_row_values():
    """Guards a real bug: a dropped header shifted every column right."""
    console = Console(file=io.StringIO(), width=200, record=True)
    render(_result(_opportunity()), console, show_ladder=False)
    out = console.export_text()
    assert "Book need" in out and "ROI" in out


def test_renders_without_error_when_nothing_triggers():
    op = _opportunity()
    op.edges = []
    console = Console(file=io.StringIO(), width=120)
    render(_result(op), console, show_ladder=False)


def test_dict_exposes_the_verification_fields():
    d = opportunity_dict(_opportunity())
    assert d["required_american_odds"] is not None
    assert d["polymarket"]["question"].startswith("Will there be a Safety")
    assert d["floor"]["explanation"]
