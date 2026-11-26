"""The feed audit and the raw stream capture. Offline."""

from datetime import UTC, datetime

from nfl_props_arb.extremes_audit import audit, our_fills, summarize
from nfl_props_arb.extremes_paper import raw_path, raw_records


def _venue_trade(tid, slug, when, passive_id, intent="ORDER_INTENT_BUY_SHORT", qty="1.0000"):
    return {"id": tid, "marketSlug": slug, "createTime": when, "qtyDecimal": qty,
            "price": {"value": "0.0100"},
            "passiveExecution": {"order": {"id": passive_id}},
            "aggressorExecution": {"order": {"id": "them", "intent": intent,
                                             "side": "ORDER_SIDE_SELL",
                                             "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"}}}


def _print(tid, slug, when, side="ORDER_SIDE_SELL", px="0.01", conn=2):
    return {"_rx": when, "_conn": conn, "trade": {
        "marketSlug": slug, "id": tid, "tradeTime": when, "price": {"value": px},
        "quantity": {"value": "1"}, "taker": {"side": side}}}


def test_raw_capture_keeps_everything_but_book_snapshots():
    stamp = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
    msgs = [{"marketData": {"marketSlug": "m"}}, {"trade": {"id": "t"}},
            {"error": "max subscriptions"}, {"_gap": {"conn": 1}}]
    out = raw_records(msgs, stamp)
    assert [next(k for k in r if k != "_rx") for r in out] == ["trade", "error", "_gap"]
    assert out[0]["_rx"] == stamp.isoformat()
    assert raw_path("data/extremes/live.jsonl", stamp).name == "raw-live-20261126T2200.jsonl"


def test_our_fills_keeps_only_trades_on_our_orders():
    trades = [_venue_trade("a", "m", "2026-11-24T02:00:00.123456789Z", "ours"),
              _venue_trade("b", "m", "2026-11-24T02:00:01Z", "someone")]
    fills = our_fills(trades, {"ours"})
    assert [f["trade_id"] for f in fills] == ["a"]
    assert fills[0]["we_were"] == "passive" and fills[0]["aggressor_intent"].endswith("SHORT")


def test_audit_tells_counted_misread_renumbered_missing_and_gapped_apart():
    t = lambda s: f"2026-11-24T02:00:{s:02d}.000000001Z"  # noqa: E731
    fills = our_fills([
        _venue_trade("c", "m1", t(1), "o1"),   # model counted it
        _venue_trade("r", "m2", t(2), "o2"),   # print arrived, model ignored it
        _venue_trade("x", "m3", t(3), "o3"),   # print arrived under another id
        _venue_trade("n", "m4", t(4), "o4"),   # nothing arrived
        _venue_trade("g", "m5", "2026-11-24T02:30:00Z", "o5"),  # while a connection was down
    ], {"o1", "o2", "o3", "o4", "o5"})
    raw = [_print("c", "m1", t(1)), _print("r", "m2", t(2), side="ORDER_SIDE_BUY"),
           _print("other", "m3", t(4))]
    gaps = [{"start": "2026-11-24T02:10:00+00:00", "end": "2026-11-24T03:05:00+00:00"}]
    rows = {r["trade_id"]: r for r in audit(fills, raw, {"c"}, gaps)}
    assert rows["c"]["verdict"] == "counted"
    assert rows["r"]["verdict"] == "on feed, not counted"
    assert rows["r"]["feed"]["raw_side"] == "ORDER_SIDE_BUY"   # the clue the audit is for
    assert rows["x"]["verdict"] == "on feed, other id"
    assert rows["n"]["verdict"] == "not on feed"
    assert rows["g"]["verdict"] == "not on feed (in gap)"
    assert summarize(list(rows.values()))["verdicts"]["counted"] == 1


def test_single_instance_lock_refuses_a_second_holder(tmp_path):
    import subprocess
    import sys

    from nfl_props_arb.runlock import lock_path, single_instance

    lock = lock_path(tmp_path / "paper.jsonl")
    assert lock.name == "paper.lock"
    probe = ("import sys; from nfl_props_arb.runlock import single_instance\n"
             "with single_instance(sys.argv[1]) as ok: print(ok)")
    with single_instance(lock) as first:
        assert first
        other = subprocess.run([sys.executable, "-c", probe, str(lock)],
                               capture_output=True, text=True, check=True)
        assert other.stdout.strip() == "False"
    after = subprocess.run([sys.executable, "-c", probe, str(lock)],
                           capture_output=True, text=True, check=True)
    assert after.stdout.strip() == "True"  # released on exit
