"""The 1-cent extremes paper trader: fill rules, queue model, settlement. Offline."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nfl_props_arb.extremes import (
    Book,
    MarketMeta,
    Tracker,
    Trade,
    bids_from_log,
    filled_rows,
    join,
    markets_from_events,
    on_depth,
    on_trade,
    parse_book,
    parse_trade,
    pnl,
    report_rows,
    settlements_from_log,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 10, 1, 23, 0, tzinfo=UTC)
META = MarketMeta(
    slug="m1", event_ticker="nfl-pit-cle-2026-10-01", game="PIT@CLE 2026-10-01",
    market_type="MONEYLINE", sports_market_type="moneyline", kickoff="2026-10-02T00:15:00Z",
    title="PIT vs CLE",
)


def _book(bids=None, offers=None, state="MARKET_STATE_OPEN", **kw) -> Book:
    return Book(slug="m1", bids=bids or {}, offers=offers or {}, state=state, **kw)


def _trade(px, qty, taker, tid="t1") -> Trade:
    return Trade(slug="m1", trade_id=tid, price=px, qty=qty, taker_side=taker,
                 trade_time="2026-10-02T01:00:00Z")


def _bids(book: Book, shares=100.0):
    yes, no = join(META, book, NOW, shares)
    return yes, no


# --- wire parsing against recorded messages ---------------------------------


def test_parses_recorded_trade_long_side_with_taker():
    msgs = json.loads((FIXTURES / "ws_trade.json").read_text(encoding="utf-8"))
    t = parse_trade(msgs[0])
    assert t is not None
    assert t.slug == "aec-nfl-atl-no-2026-10-05"
    assert t.price == pytest.approx(0.4325)
    assert t.qty == pytest.approx(42.52)
    assert t.taker_side == "BUY"
    sells = [parse_trade(m) for m in msgs]
    assert any(s is not None and s.taker_side == "SELL" for s in sells)


def test_parses_recorded_book_snapshot():
    msgs = json.loads((FIXTURES / "ws_market_data.json").read_text(encoding="utf-8"))
    book = parse_book(msgs[0])
    assert book is not None
    assert book.state == "MARKET_STATE_OPEN"
    assert book.depth("YES") == pytest.approx(12.0)       # bid 0.01 x 12
    assert book.depth("NO") == pytest.approx(110001.0)    # offer 0.99 x 110001
    assert book.low_px == pytest.approx(0.05)
    assert parse_trade(msgs[0]) is None
    assert parse_book(msgs[1]) is None  # the LITE message is not a book


# --- joining -------------------------------------------------------------------


def test_join_sits_behind_existing_depth_at_our_price_only():
    book = _book(bids={0.01: 500.0, 0.02: 99.0, 0.005: 1e6}, offers={0.99: 70.0, 0.5: 3.0})
    yes, no = _bids(book)
    assert (yes.side, yes.ahead, yes.queue_at_join) == ("YES", 500.0, 500.0)
    assert (no.side, no.ahead) == ("NO", 70.0)
    assert not yes.would_cross and not no.would_cross


def test_would_cross_is_flagged_not_dropped():
    yes, no = _bids(_book(bids={0.99: 5.0}, offers={0.01: 5.0}))
    assert yes.would_cross and no.would_cross


# --- fill rules -----------------------------------------------------------------


def test_print_at_one_cent_with_a_buying_taker_cannot_touch_our_bid():
    yes, _ = _bids(_book())
    assert not on_trade(yes, _trade(0.01, 1000, "BUY"))
    assert yes.optimistic_filled == 0 and yes.first_touch is None


def test_yes_bid_queue_aware_needs_depth_ahead_to_trade_first():
    yes, _ = _bids(_book(bids={0.01: 150.0}))
    on_trade(yes, _trade(0.01, 100, "SELL", "a"))
    assert yes.optimistic_filled == 100
    assert yes.queue_filled == 0 and yes.ahead == 50
    on_trade(yes, _trade(0.01, 80, "SELL", "b"))
    assert yes.queue_filled == 30 and yes.ahead == 0
    on_trade(yes, _trade(0.01, 500, "SELL", "c"))
    assert yes.queue_filled == 100  # capped at our size


def test_no_bid_is_a_long_ask_at_99_hit_by_buyers():
    _, no = _bids(_book(offers={0.99: 0.0}))
    assert not on_trade(no, _trade(0.99, 50, "SELL"))
    assert on_trade(no, _trade(0.99, 40, "BUY"))
    assert no.optimistic_filled == 40 and no.queue_filled == 40


def test_print_through_our_price_fills_in_full():
    yes, _ = _bids(_book(bids={0.01: 1e6}))
    on_trade(yes, _trade(0.005, 1, "SELL"))
    assert yes.optimistic_filled == 100 and yes.queue_filled == 100


def test_depth_shrinking_moves_us_up_but_added_depth_does_not_push_us_back():
    yes, _ = _bids(_book(bids={0.01: 1000.0}))
    on_depth(yes, _book(bids={0.01: 300.0}))
    assert yes.ahead == 300
    on_depth(yes, _book(bids={0.01: 5000.0}))
    assert yes.ahead == 300


# --- settlement ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "settle", "expected"),
    [("YES", 1.0, 99.0), ("YES", 0.0, -1.0), ("NO", 0.0, 99.0), ("NO", 1.0, -1.0)],
)
def test_pnl_is_a_dollar_per_share_less_a_cent(side, settle, expected):
    assert pnl(100, side, settle) == pytest.approx(expected)


# --- tracker, log replay, report -------------------------------------------------


def _md(slug, bids, offers, state="MARKET_STATE_OPEN"):
    lvl = lambda d: [{"px": {"value": str(p)}, "qty": str(q)} for p, q in d.items()]  # noqa: E731
    return {"marketData": {"marketSlug": slug, "bids": lvl(bids), "offers": lvl(offers),
                           "state": state}}


def _tm(slug, px, qty, side, tid):
    return {"trade": {"marketSlug": slug, "price": {"value": str(px)},
                      "quantity": {"value": str(qty)}, "taker": {"side": f"ORDER_SIDE_{side}"},
                      "id": tid, "tradeTime": "2026-10-02T01:00:00Z"}}


def test_tracker_end_to_end_with_replay_and_report():
    t = Tracker(shares=100)
    t.add_markets([META])
    log = []
    log += t.handle(_md("m1", {0.01: 20.0}, {0.5: 1.0}), NOW)
    assert [r["event"] for r in log] == ["state", "join", "join"]
    log += t.handle(_tm("m1", 0.01, 70, "SELL", "x1"), NOW)
    log += t.handle(_tm("m1", 0.01, 70, "SELL", "x1"), NOW)  # duplicate id ignored
    yes = t.bids[("m1", "YES")]
    assert yes.optimistic_filled == 70 and yes.queue_filled == 50

    rebuilt = bids_from_log(log)
    assert rebuilt[("m1", "YES")].queue_filled == 50
    assert rebuilt[("m1", "YES")].ahead == 0

    log.append({"event": "settle", "slug": "m1", "settlement_px": 1.0})
    rows = report_rows(rebuilt, settlements_from_log(log))
    yes_row = next(r for r in rows if r["side"] == "YES")
    assert yes_row["queue_fills"] == 1 and yes_row["queue_wins"] == 1
    assert yes_row["queue_pnl"] == pytest.approx(50 * 0.99)
    assert yes_row["optimistic_pnl"] == pytest.approx(70 * 0.99)
    assert filled_rows(rebuilt, {"m1": 1.0})[0]["queue_pnl"] == pytest.approx(49.5)


def test_expired_market_takes_no_bids():
    t = Tracker()
    t.add_markets([META])
    out = t.handle(_md("m1", {}, {}, state="MARKET_STATE_EXPIRED"), NOW)
    assert [r["event"] for r in out] == ["state"]
    assert t.open_slugs() == []


def test_markets_from_events_takes_every_game_market_and_skips_futures(nfl_event):
    events = [nfl_event["events"][0] if "events" in nfl_event else nfl_event,
              {"ticker": "nfl-super-bowl-winner", "markets": [{"slug": "fut"}]}]
    metas = markets_from_events(events)
    assert len(metas) == 8
    assert all(m.market_type == "PROP" for m in metas)
    assert "fut" not in {m.slug for m in metas}
