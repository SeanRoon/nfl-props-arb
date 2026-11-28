"""The live 1-cent extremes trader: decisions, the shadow model, the order stream. Offline."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nfl_props_arb.extremes import MarketMeta, Tracker
from nfl_props_arb.extremes_live import (
    DEFAULT_SKIP,
    LiveOrder,
    LiveTracker,
    compare_groups,
    compare_rows,
    parse_skip,
)
from nfl_props_arb.extremes_live_run import dry_ack, live_logs

NOW = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)


def _meta(slug="m1", mtype="PROP"):
    return MarketMeta(slug=slug, event_ticker="nfl-gb-det-2026-11-26", game="GB@DET 2026-11-26",
                      market_type=mtype, sports_market_type="x",
                      kickoff="2026-11-26T21:30:00Z", title=f"{slug} title")


def _md(slug, bids, offers, state="MARKET_STATE_OPEN"):
    lvl = lambda d: [{"px": {"value": str(p)}, "qty": str(q)} for p, q in d.items()]  # noqa: E731
    return {"marketData": {"marketSlug": slug, "bids": lvl(bids), "offers": lvl(offers),
                           "state": state}}


def _tm(slug, px, qty, side, tid):
    return {"trade": {"marketSlug": slug, "price": {"value": str(px)},
                      "quantity": {"value": str(qty)}, "taker": {"side": f"ORDER_SIDE_{side}"},
                      "id": tid, "tradeTime": "2026-11-26T22:00:00Z"}}


def _upd(order_id, state, cum, exec_id):
    return {"orderSubscriptionUpdate": {"execution": {
        "id": exec_id, "order": {"id": order_id, "state": state, "cumQuantity": str(cum)}}}}


def _tracker(*metas, **kw):
    t = LiveTracker(**kw)
    t.add_markets(list(metas) or [_meta()])
    return t


def _place_all(t, intents, now=NOW):
    """Place and ack every intent, as the dry-run worker does. Returns log records."""
    out = []
    for i in intents:
        oid = f"o-{i.slug}-{i.side}"
        out += t.placed(i, oid, now)
        out += t.handle_private(dry_ack(oid), now)
    return out


# --- decisions -----------------------------------------------------------------


def test_first_open_book_decides_both_sides_once():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {0.01: 500}, {0.99: 800}), NOW)
    assert {(i.side, i.long_price) for i in intents} == {("YES", 0.01), ("NO", 0.99)}
    assert all(i.expires_at == datetime(2026, 11, 27, 21, 30, tzinfo=UTC) for i in intents)
    _, again = t.handle_market(_md("m1", {0.01: 500}, {0.99: 800}), NOW)
    assert again == []  # decided once, like the paper join


def test_default_skip_leaves_out_spread_and_total_no():
    t = _tracker(_meta("s", "SPREAD"), _meta("t", "TOTAL"), _meta("p", "PROP"))
    sides = set()
    for slug in ("s", "t", "p"):
        _, intents = t.handle_market(_md(slug, {0.01: 1}, {0.99: 1}), NOW)
        sides |= {(t.metas[i.slug].market_type, i.side) for i in intents}
    assert sides == {("SPREAD", "YES"), ("TOTAL", "YES"), ("PROP", "YES"), ("PROP", "NO")}
    assert DEFAULT_SKIP == parse_skip(["spread:no", "TOTAL:NO"])


def test_parse_skip_refuses_malformed_slices():
    with pytest.raises(ValueError):
        parse_skip(["SPREAD-NO"])


def test_would_cross_is_skipped_for_good_and_logged():
    t = _tracker()
    # An offer at 0.01 means a YES bid at 0.01 would take it: post-only refuses.
    recs, intents = t.handle_market(_md("m1", {}, {0.01: 5}), NOW)
    assert [i.side for i in intents] == ["NO"]
    assert [r["reason"] for r in recs if r["event"] == "skip"] == ["would_cross"]
    _, later = t.handle_market(_md("m1", {}, {0.5: 5}), NOW)
    assert later == []  # the paper trader never re-joins either


def test_non_open_market_is_not_decided_until_it_opens():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {}, {}, state="MARKET_STATE_HALTED"), NOW)
    assert intents == []
    _, intents = t.handle_market(_md("m1", {}, {}), NOW)
    assert len(intents) == 2


def test_exposure_cap_holds_and_frees_when_a_market_finishes():
    t = _tracker(_meta("a"), _meta("b"), max_exposure=0.02)  # two 1-share bids
    _, ia = t.handle_market(_md("a", {}, {}), NOW)
    recs, ib = t.handle_market(_md("b", {}, {}), NOW)
    assert len(ia) == 2 and ib == []
    assert {r["reason"] for r in recs if r["event"] == "skip"} == {"cap"}
    assert t.exposure == pytest.approx(0.02)
    _place_all(t, ia)
    t.handle_market(_md("a", {}, {}, state="MARKET_STATE_EXPIRED"), NOW)
    assert t.exposure == pytest.approx(0.0)
    _, ib = t.handle_market(_md("b", {}, {}), NOW)
    assert len(ib) == 2  # a cap skip is retried; the others are final


def test_rejected_order_releases_its_exposure():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {}, {}), NOW)
    t.failed(intents[0], "rejected", "HTTP 400", NOW)
    assert t.exposure == pytest.approx(0.01)


# --- the shadow ------------------------------------------------------------------


def test_shadow_joins_at_ack_behind_the_queue_but_not_behind_itself():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {0.01: 500}, {0.99: 800}), NOW)
    assert t.shadows == {}  # deciding is not resting
    # By the time the ack lands, the queue has grown and includes our own share.
    t.handle_market(_md("m1", {0.01: 601}, {0.99: 801}), NOW)
    recs = _place_all(t, intents)
    joins = {r["side"]: r for r in recs if r["event"] == "join"}
    assert joins["YES"]["queue_at_join"] == 600 and joins["NO"]["queue_at_join"] == 800
    assert joins["YES"]["order_id"] == "o-m1-YES"


def test_shadow_matches_the_paper_model_on_the_same_queue():
    """Same market, same queue position -> the same queue-aware fill.

    The live book is the paper book plus our own resting share at our price:
    the paper trader's bid is virtual, the live one is really in the book.
    """
    paper = Tracker(shares=1)
    paper.add_markets([_meta()])
    t = _tracker()
    paper.handle(_md("m1", {0.01: 30}, {0.99: 40}), NOW)
    _, intents = t.handle_market(_md("m1", {0.01: 30}, {0.99: 40}), NOW)
    t.handle_market(_md("m1", {0.01: 31}, {0.99: 41}), NOW)  # our shares now rest
    _place_all(t, intents)
    paper_book = _md("m1", {0.01: 20}, {0.99: 40})   # 10 ahead of YES left without trading
    live_book = _md("m1", {0.01: 21}, {0.99: 41})
    trades = [
        _tm("m1", 0.01, 15, "SELL", "t1"),        # 15 of the 20 left ahead
        _tm("m1", 0.99, 39, "BUY", "t2"),         # NO: 1 short of us
        _tm("m1", 0.01, 7, "SELL", "t3"),         # the last 5 ahead, then us
        _tm("m1", 0.01, 7, "SELL", "t3"),         # duplicate print
    ]
    paper.handle(paper_book, NOW)
    t.handle_market(live_book, NOW)
    for m in trades:
        paper.handle(m, NOW)
        t.handle_market(m, NOW)
    for side in ("YES", "NO"):
        p, s = paper.bids[("m1", side)], t.shadows[("m1", side)]
        assert (s.queue_filled, s.ahead) == (p.queue_filled, p.ahead), side
    assert t.shadows[("m1", "YES")].queue_filled == 1
    assert t.shadows[("m1", "NO")].queue_filled == 0


def test_ack_before_any_book_joins_at_the_next_snapshot():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {}, {}), NOW)
    t.books.clear()
    _place_all(t, intents)
    assert t.shadows == {} and len(t.pending_join) == 2
    recs, _ = t.handle_market(_md("m1", {0.01: 9}, {}), NOW)
    assert sum(r["event"] == "join" for r in recs) == 2
    assert t.shadows[("m1", "YES")].queue_at_join == 8


def test_ack_that_beats_the_batch_reply_is_held_then_applied():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {}, {}), NOW)
    yes = next(i for i in intents if i.side == "YES")
    assert t.handle_private(_upd("o9", "ORDER_STATE_NEW", 0, "e1"), NOW) == []
    recs = t.placed(yes, "o9", NOW)
    assert [r["event"] for r in recs] == ["placed", "order", "join"]


def test_real_fill_is_recorded_but_does_not_stop_the_shadow():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {0.01: 5}, {}), NOW)
    _place_all(t, intents)
    recs = t.handle_private(_upd("o-m1-YES", "ORDER_STATE_FILLED", 1, "e2"), NOW)
    assert recs[0]["cum_qty"] == 1
    assert t.handle_private(_upd("o-m1-YES", "ORDER_STATE_FILLED", 1, "e2"), NOW) == []
    assert ("m1", "YES") not in t.stopped
    assert t.orders[("m1", "YES")].unfilled == 0


def test_venue_cancel_stops_the_shadow():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {0.01: 5}, {}), NOW)
    _place_all(t, intents)
    recs = t.handle_private(_upd("o-m1-YES", "ORDER_STATE_CANCELED", 0, "e3"), NOW)
    assert recs[-1]["event"] == "shadow_stop"
    out, _ = t.handle_market(_tm("m1", 0.01, 99, "SELL", "t9"), NOW)
    assert out == [] and t.shadows[("m1", "YES")].queue_filled == 0
    assert t.exposure == pytest.approx(0.01)  # the NO bid still rests


def test_partially_filled_order_still_rests():
    o = LiveOrder("m", "YES", 2.0, status="placed", state="ORDER_STATE_PARTIALLY_FILLED",
                  cum_qty=1.0)
    assert o.live and o.unfilled == 1.0


# --- restart and report ----------------------------------------------------------


def test_replay_never_places_twice_and_restores_everything():
    t = _tracker()
    log = []
    recs, intents = t.handle_market(_md("m1", {0.01: 3}, {0.99: 4}), NOW)
    log += recs
    log += [t.intent_record(i, NOW) for i in intents]
    log += _place_all(t, intents)
    r, _ = t.handle_market(_tm("m1", 0.01, 10, "SELL", "t1"), NOW)
    log += r
    log += t.handle_private(_upd("o-m1-YES", "ORDER_STATE_FILLED", 1, "e5"), NOW)

    back = _tracker()
    back.replay(json.loads(json.dumps(log, default=str)))
    _, again = back.handle_market(_md("m1", {0.01: 3}, {0.99: 4}), NOW)
    assert again == []
    assert back.orders[("m1", "YES")].cum_qty == 1
    assert back.shadows[("m1", "YES")].queue_filled == 1
    assert back.exposure == pytest.approx(t.exposure)
    assert back.handle_private(_upd("o-m1-YES", "ORDER_STATE_FILLED", 1, "e5"), NOW) == []


def test_intent_without_reply_is_never_retried_after_restart():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {}, {}), NOW)
    log = [t.intent_record(i, NOW) for i in intents]  # crashed before the reply
    back = _tracker()
    back.replay(log)
    _, again = back.handle_market(_md("m1", {}, {}), NOW)
    assert again == [] and back.exposure == pytest.approx(0.02)


def test_compare_reports_real_and_shadow_pnl_side_by_side():
    t = _tracker()
    _, intents = t.handle_market(_md("m1", {0.01: 0}, {0.99: 0}), NOW)
    _place_all(t, intents)
    t.handle_market(_tm("m1", 0.01, 1, "SELL", "t1"), NOW)          # shadow YES fills
    t.handle_private(_upd("o-m1-NO", "ORDER_STATE_FILLED", 1, "e1"), NOW)  # real NO fills
    rows = compare_rows(t, {"m1": 1.0})
    by = {r["side"]: r for r in rows}
    assert by["YES"]["shadow_pnl"] == pytest.approx(0.99) and by["YES"]["real_pnl"] == 0
    assert by["NO"]["real_pnl"] == pytest.approx(-0.01) and by["NO"]["shadow_pnl"] == 0
    assert not by["YES"]["agree"] and not by["NO"]["agree"]
    g = {x["side"]: x for x in compare_groups(rows)}
    assert g["YES"]["shadow_only"] == 1 and g["NO"]["real_only"] == 1


# --- the venue client ------------------------------------------------------------


pytest.importorskip("cryptography")


def _client(handler):
    from nfl_props_arb.execution.client import LiveOrderClient
    from nfl_props_arb.execution.credentials import Credentials

    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return LiveOrderClient(Credentials(key_id="kid", private_key=bytes(range(32))), client=http)


def _bids(n=2):
    from nfl_props_arb.execution.client import RestingBid

    exp = NOW + timedelta(hours=24)
    sides = [("YES", 0.01), ("NO", 0.99)]
    return [RestingBid(f"m{k}", *sides[k % 2], 1.0, exp) for k in range(n)]


def test_batch_wire_format_prices_both_sides_long():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"createdOrderIds": ["a", "b"]})

    result = _client(handler).place_resting_batch(_bids())
    assert result.status == "accepted" and result.order_ids == ["a", "b"]
    assert seen["path"] == "/v1/orders/batched"
    yes, no = seen["body"]["orders"]
    assert (yes["intent"], yes["price"]["value"]) == ("ORDER_INTENT_BUY_LONG", "0.01")
    assert (no["intent"], no["price"]["value"]) == ("ORDER_INTENT_BUY_SHORT", "0.99")
    assert yes["participateDontInitiate"] is True
    assert yes["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE"
    assert yes["goodTillTime"] == "2026-11-27T20:00:00Z"


@pytest.mark.parametrize(
    ("code", "payload", "status"),
    [
        (400, {"message": "bad"}, "rejected"),
        (500, {}, "unknown"),
        (409, {}, "unknown"),
        (200, {"createdOrderIds": ["only-one"]}, "unknown"),
    ],
)
def test_batch_failures_are_refused_or_ambiguous_never_accepted(code, payload, status):
    result = _client(lambda r: httpx.Response(code, json=payload)).place_resting_batch(_bids())
    assert result.status == status


def test_batch_size_is_bounded():
    from nfl_props_arb.execution.client import OrderSpecError

    with pytest.raises(OrderSpecError):
        _client(lambda r: httpx.Response(200)).place_resting_batch(_bids(21))


def test_quotas_count_replayed_bids_and_shut_unlisted_slices():
    from nfl_props_arb.extremes_live import parse_quotas

    metas = [_meta(f"p{k}", "PROP") for k in range(3)] + [_meta("s0", "SPREAD")]
    first = _tracker(*metas)
    _, intents = first.handle_market(_md("p0", {}, {}), NOW)  # PROP YES + NO, no quotas
    log = [first.intent_record(i, NOW) for i in intents]

    quotas = parse_quotas(["PROP:YES=2", "PROP:NO=1"])
    t = _tracker(*metas, quotas=quotas)
    t.replay(log)
    got = []
    for slug in ("p1", "p2", "s0"):
        _, new = t.handle_market(_md(slug, {}, {}), NOW)
        got += [(i.slug, i.side) for i in new]
    # PROP:YES had 1 of 2 already; PROP:NO was full; SPREAD has no quota at all.
    assert got == [("p1", "YES")]
    with pytest.raises(ValueError):
        parse_quotas(["PROP:YES"])


def test_keys_from_an_earlier_log_repeats_exactly_those_bids():
    from nfl_props_arb.extremes_live import intent_keys

    metas = [_meta("a"), _meta("b")]
    first = _tracker(*metas)
    _, intents = first.handle_market(_md("a", {}, {}), NOW)
    earlier = [first.intent_record(i, NOW) for i in intents if i.side == "NO"]

    t = _tracker(*metas, allowed_keys=intent_keys(earlier))
    got = []
    for slug in ("a", "b"):
        _, new = t.handle_market(_md(slug, {}, {}), NOW)
        got += [(i.slug, i.side) for i in new]
    assert got == [("a", "NO")]


def test_keep_awake_sets_then_clears_the_execution_state(monkeypatch):
    import nfl_props_arb.awake as awake

    calls = []
    monkeypatch.setattr(awake, "_set", lambda flags: calls.append(flags) or True)
    with awake.keep_awake() as ok:
        assert ok
    assert calls == [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED, awake.ES_CONTINUOUS]


def test_live_logs_finds_per_game_logs_but_not_sidecars(tmp_path):
    names = ["live.jsonl", "live-dryrun.jsonl", "live-kc-buf.jsonl", "live-kc-buf-reconcile.jsonl",
             "live-reconcile.jsonl", "live-venue-trades.jsonl", "live-kc-buf.lock",
             "paper.jsonl", "raw-live-kc-buf-20261126T0149.jsonl", "settlements.jsonl"]
    for n in names:
        (tmp_path / n).write_text("")
    assert [p.name for p in live_logs(tmp_path)] == [
        "live-dryrun.jsonl", "live-kc-buf.jsonl", "live.jsonl"]
