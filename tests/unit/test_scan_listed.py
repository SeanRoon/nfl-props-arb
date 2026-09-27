"""The report leaves out markets Polymarket US does not list on the event page."""

from datetime import UTC, datetime

from nfl_props_arb.combine import Floor
from nfl_props_arb.edge import Edge
from nfl_props_arb.polymarket.client import PmProp
from nfl_props_arb.props import GameKey, PropKey, PropType, Scope
from nfl_props_arb.scan import Opportunity, ScanResult, listed_only

GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")


def _opportunity(key: PropKey, slug: str) -> Opportunity:
    prop = PmProp(
        key=key,
        slug=slug,
        market_id=slug,
        question="",
        title="",
        theta=0.06,
        line=0.5,
        event_ticker="nfl-det-buf-2026-09-17",
        start_date="2026-09-18T00:15:00Z",
        best_yes_bid=0.30,
    )
    return Opportunity(
        prop=prop,
        floor=Floor(value=0.79, method="direct"),
        max_buy=0.78,
        edges=[Edge(no_price=0.70, floor=0.79, theta=0.06, qty=5.0)],
    )


TEAM_DST = _opportunity(PropKey(GAME, PropType.DST_TD, Scope.TEAM, "det"), "tdstd-det")
GAME_DST = _opportunity(PropKey(GAME, PropType.DST_TD, Scope.GAME), "tdstd")
SAFETY = _opportunity(PropKey(GAME, PropType.SAFETY, Scope.GAME), "safety")


def _result() -> ScanResult:
    return ScanResult(
        [TEAM_DST, GAME_DST, SAFETY],
        considered=5,
        unpriced=[
            {"key": "k1", "slug": "tdstd-buf", "reason": "no_fanduel_quote", "listed": False},
            {"key": "k2", "slug": "ot", "reason": "no_fanduel_quote", "listed": True},
        ],
        generated_at=datetime.now(UTC),
    )


def test_team_dst_rows_are_dropped_and_whole_game_rows_kept():
    shown = listed_only(_result())
    assert [o.prop.slug for o in shown.opportunities] == ["tdstd", "safety"]
    assert [u["slug"] for u in shown.unpriced] == ["ot"]
    assert shown.considered == 4


def test_the_full_result_is_left_intact_for_autotrade():
    full = _result()
    listed_only(full)
    assert len(full.opportunities) == 3
    assert len(full.unpriced) == 2
