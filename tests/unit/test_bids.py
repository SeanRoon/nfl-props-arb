"""Resting bids: which markets get one, at what price, and when it expires."""

from datetime import UTC, datetime, timedelta

from nfl_props_arb.baselines import DEFAULT_AUTOTRADE
from nfl_props_arb.bids import fit_to_budget, placed_order_ids, plan_bids
from nfl_props_arb.polymarket.client import PmProp
from nfl_props_arb.props import GameKey, PropKey, PropType, Scope

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")
BUFFER = timedelta(minutes=15)


def _prop(slug="m-2pt", prop=PropType.TWO_PT, kickoff="2026-09-27T17:00:00Z", no_ask=0.75,
          scope=Scope.GAME, team=None):
    return PmProp(
        key=PropKey(game=GAME, prop=prop, scope=scope, team=team),
        slug=slug, market_id="1", question="q", title="t", theta=0.0695, line=0.5,
        event_ticker="e", start_date=kickoff,
        best_yes_bid=None if no_ask is None else 1.0 - no_ask,
    )


def _plan(props, **kw):
    args = {"shares": 50.0, "now": NOW, "kickoff_buffer": BUFFER, "open_slugs": set()}
    args.update(kw)
    return plan_bids(props, DEFAULT_AUTOTRADE, **args)


def test_bid_is_at_max_buy_and_expires_before_kickoff():
    (bid,), _ = _plan([_prop()])
    assert (bid.no_price, bid.shares) == (0.69, 50.0)
    assert bid.expires_at == datetime(2026, 9, 27, 16, 45, tzinfo=UTC)


def test_started_or_imminent_games_get_no_bid():
    planned, skipped = _plan([_prop(kickoff="2026-09-24T12:10:00Z")])
    assert planned == [] and skipped[0]["reason"] == "started"


def test_unparseable_kickoff_gets_no_bid():
    planned, skipped = _plan([_prop(kickoff="soon")])
    assert planned == [] and skipped[0]["reason"] == "started"


def test_overtime_is_never_bid():
    planned, skipped = _plan([_prop(prop=PropType.OVERTIME)])
    assert planned == [] and skipped[0]["reason"] == "not_tradeable"


def test_a_market_with_an_open_order_is_not_stacked():
    planned, skipped = _plan([_prop()], open_slugs={"m-2pt"})
    assert planned == [] and skipped[0]["reason"] == "open_order_exists"


def test_a_takeable_offer_is_left_to_the_autotrader():
    planned, skipped = _plan([_prop(no_ask=0.69)])
    assert planned == [] and skipped[0]["reason"] == "would_cross"


def test_an_empty_book_still_gets_a_bid():
    planned, _ = _plan([_prop(no_ask=None)])
    assert len(planned) == 1


def test_price_override_below_the_limit_is_used():
    (bid,), _ = _plan([_prop()], price=0.65)
    assert bid.no_price == 0.65


def test_price_override_above_the_limit_is_refused():
    planned, skipped = _plan([_prop()], price=0.72)
    assert planned == [] and skipped[0]["reason"] == "above_max_buy"


def test_prop_filter():
    props = [_prop("a", PropType.TWO_PT), _prop("b", PropType.SAFETY, no_ask=0.9)]
    planned, _ = _plan(props, only={PropType.TWO_PT})
    assert [b.slug for b in planned] == ["a"]


def test_each_prop_gets_its_own_limit():
    (bid,), _ = _plan([_prop("s", PropType.SAFETY, no_ask=0.9)])
    assert bid.no_price == 0.83


def test_team_scoped_dst_markets_are_bid_too():
    (bid,), _ = _plan([_prop("d", PropType.DST_TD, scope=Scope.TEAM, team="det", no_ask=0.85)])
    assert (bid.team, bid.no_price) == ("det", 0.69)


def test_budget_is_checked_per_bid_not_summed():
    """The venue checks collateral per instrument, so every affordable bid rests."""
    props = [_prop("a"), _prop("b"), _prop("c")]
    planned, _ = _plan(props)
    kept, dropped = fit_to_budget(planned, 40.0)   # each bid reserves 34.50
    assert [b.slug for b in kept] == ["a", "b", "c"] and dropped == []


def test_a_bid_bigger_than_buying_power_is_dropped():
    planned, _ = _plan([_prop()])
    kept, dropped = fit_to_budget(planned, 30.0)
    assert kept == [] and dropped == [{"slug": "m-2pt", "reason": "buying_power"}]


def test_placed_ids_come_only_from_placed_events(tmp_path):
    path = tmp_path / "bids.jsonl"
    path.write_text(
        '{"event":"placed","order_id":"A"}\n{"event":"rejected","order_id":null}\nnot json\n',
        encoding="utf-8",
    )
    assert placed_order_ids(path) == {"A"}
