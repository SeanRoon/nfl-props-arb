"""Polymarket US parsing, against recorded live responses.

Offline by convention: these run against fixtures captured on 2026-09-16, never
against the network.
"""

import json

import pytest

from nfl_props_arb.edge import no_levels_from_yes_bids
from nfl_props_arb.polymarket.client import PolymarketUS, _team_from_slug
from nfl_props_arb.props import GameKey, PropType, Scope


@pytest.fixture
def client():
    return PolymarketUS.__new__(PolymarketUS)  # parsing only, no HTTP


def test_extracts_every_target_prop(client, nfl_event):
    props, skipped = client.props_from_events(nfl_event["events"])
    assert not skipped
    assert {p.key.prop for p in props} == set(PropType)


def test_team_scoped_dst_td_covers_both_teams(client, nfl_event):
    props, _ = client.props_from_events(nfl_event["events"])
    teams = {p.key.team for p in props if p.key.scope is Scope.TEAM}
    assert teams == {"det", "buf"}


def test_game_scoped_props_carry_no_team(client, nfl_event):
    props, _ = client.props_from_events(nfl_event["events"])
    assert all(p.key.team is None for p in props if p.key.scope is Scope.GAME)


def test_futures_events_are_ignored(client):
    event = {"ticker": "nfl-afcwest-2027-01-10-w", "markets": [{"sportsMarketType": "futures"}]}
    props, _ = client.props_from_events([event])
    assert props == []


def test_non_half_lines_are_skipped(client, nfl_event):
    """An Over 1.5 market asks for two events, not one, and must not be priced."""
    event = json.loads(json.dumps(nfl_event["events"][0]))
    for market in event["markets"]:
        market["line"] = 1.5
    props, skipped = client.props_from_events([event])
    assert props == []
    assert {s["reason"] for s in skipped} == {"line"}


def test_outcomes_order_is_not_trusted(nfl_event):
    """`outcomes` ordering varies per market, so positional indexing is unsafe.

    Live sample: 59 markets listed ["No","Yes"] and 69 listed ["Yes","No"].
    The long-side flag is the reliable signal, and bestBid is always YES-terms.
    """
    orders = {
        tuple(json.loads(m["outcomes"])) for m in nfl_event["events"][0]["markets"]
    }
    assert len(orders) > 1, "fixture should contain both orderings"
    for market in nfl_event["events"][0]["markets"]:
        long_side = next(s for s in market["marketSides"] if s["long"] is True)
        assert long_side["description"] == "Yes"


def test_no_quote_equals_one_minus_best_bid(nfl_event):
    """The invariant the NO ladder derivation depends on."""
    for market in nfl_event["events"][0]["markets"]:
        best_bid = market.get("bestBidQuote")
        if not best_bid:
            continue
        short = next(s for s in market["marketSides"] if s["long"] is False)
        assert float(short["quote"]["value"]) == pytest.approx(
            1 - float(best_bid["value"]), abs=1e-6
        )


def test_book_bids_become_the_no_ladder(pm_book):
    bids = [
        (float(b["px"]["value"]), float(b["qty"]))
        for b in pm_book["marketData"]["bids"]
    ]
    levels = no_levels_from_yes_bids(bids)
    assert levels[0].price == pytest.approx(1 - max(px for px, _ in bids))
    assert all(a.price <= b.price for a, b in zip(levels, levels[1:], strict=False))


def test_team_recovered_from_slug():
    game = GameKey.from_ticker("nfl-det-buf-2026-09-17")
    slug = "astatc-nfl-det-buf-2026-09-17-tdstd-buf-0pt5"
    assert _team_from_slug(slug, game) == "buf"


def test_team_slug_not_confused_by_the_leading_matchup():
    """The away-home pair appears earlier in every slug; the tail is what counts."""
    game = GameKey.from_ticker("nfl-det-buf-2026-09-17")
    assert _team_from_slug("astatc-nfl-det-buf-2026-09-17-tdstd-det-0pt5", game) == "det"
