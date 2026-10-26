"""Polymarket US parsing, against recorded live responses.

Offline by convention: these run against fixtures captured on 2026-09-16, never
against the network.
"""

import json

import httpx
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


# --- truncated event listing (2026-10-15 onward) ------------------------------
#
# The tagSlug listing began embedding only each event's Primary market while
# `marketCounts.numMarkets` still reported the full count. These run against a
# mock transport, so they stay offline.




def _listing_client(listing, by_slug):
    """A real PolymarketUS over a mock transport that records every request."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        slug = request.url.params.get("slug")
        if slug is None:
            return httpx.Response(200, json={"events": listing})
        return httpx.Response(200, json={"events": by_slug.get(slug, [])})

    http = httpx.Client(base_url="https://test", transport=httpx.MockTransport(handler))
    return PolymarketUS(client=http), seen


def _truncated(event):
    """The event as the listing now returns it: one market, full count reported."""
    short = json.loads(json.dumps(event))
    short["marketCounts"] = {"numMarkets": len(event["markets"])}
    short["markets"] = event["markets"][:1]
    return short


def test_truncated_event_is_refetched_by_slug(nfl_event):
    full = nfl_event["events"][0]
    assert len(full["markets"]) > 1
    pm, seen = _listing_client([_truncated(full)], {full["slug"]: [full]})

    events = pm.nfl_events("2026-09-17T00:00:00Z", "2026-09-18T00:00:00Z")

    assert len(events[0]["markets"]) == len(full["markets"])
    assert [p.get("slug") for p in seen] == [None, full["slug"]]
    props, _ = pm.props_from_events(events)
    assert {p.key.prop for p in props} == set(PropType)


def test_complete_event_makes_no_extra_request(nfl_event):
    full = json.loads(json.dumps(nfl_event["events"][0]))
    full["marketCounts"] = {"numMarkets": len(full["markets"])}
    pm, seen = _listing_client([full], {})

    events = pm.nfl_events("2026-09-17T00:00:00Z", "2026-09-18T00:00:00Z")

    assert len(events[0]["markets"]) == len(full["markets"])
    assert len(seen) == 1


def test_event_without_market_counts_is_left_alone(nfl_event):
    """Older payloads carry no marketCounts; nothing to compare, so no refetch."""
    event = json.loads(json.dumps(nfl_event["events"][0]))
    event.pop("marketCounts", None)
    pm, seen = _listing_client([event], {})

    pm.nfl_events("2026-09-17T00:00:00Z", "2026-09-18T00:00:00Z")

    assert len(seen) == 1


def test_refetch_that_comes_back_empty_warns_and_keeps_listing(nfl_event):
    short = _truncated(nfl_event["events"][0])
    pm, _ = _listing_client([short], {})

    with pytest.warns(RuntimeWarning, match="returned nothing"):
        events = pm.nfl_events("2026-09-17T00:00:00Z", "2026-09-18T00:00:00Z")

    assert len(events[0]["markets"]) == 1


def test_refetch_still_short_warns(nfl_event):
    full = nfl_event["events"][0]
    short = _truncated(full)
    short["marketCounts"]["numMarkets"] = len(full["markets"]) + 10
    pm, _ = _listing_client([short], {full["slug"]: [full]})

    with pytest.warns(RuntimeWarning, match="by-slug fetch has"):
        events = pm.nfl_events("2026-09-17T00:00:00Z", "2026-09-18T00:00:00Z")

    assert len(events[0]["markets"]) == len(full["markets"])
