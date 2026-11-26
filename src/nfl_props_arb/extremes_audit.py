"""Audit the public trade feed against the venue's record of our real fills.

Found 2026-11-24 (CIN@WAS, 105 live 1-cent bids): the venue filled 55 orders
across 65 trades, but only 8 of those trades ever reached the models -- the
paper trader's connections and the live trader's saw the same 8. The models
log a trade only when it hits a bid of theirs, so the rest left no trace, and
the cause could not be told apart: never sent, or sent and misread.

The traders now keep a raw capture of every non-book stream message
(`raw-*.jsonl`, `extremes_paper.raw_records`). For each real fill this module
asks, in order:

* **counted** -- the shadow model logged the trade against our bid;
* **on feed, not counted** -- the trade print arrived but the model did not
  take it as a hit (wrong side, price, or quantity in the model's reading);
* **on feed, other id** -- no print with this trade id, but one in the same
  market within a couple of seconds (the feed may number trades differently);
* **not on feed** -- nothing arrived, while a connection covering it was up
  (or `in gap`, while one was down).

Pure: no network, no disk.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from .extremes import parse_trade

NEAR = timedelta(seconds=2)


def _t(ts: str) -> datetime | None:
    """ISO-8601 with nanoseconds and Z, or microseconds and +00:00, to a datetime."""
    if not ts:
        return None
    text = ts.replace("Z", "+00:00")
    head, _, tz = text.partition("+")
    if "." in head:
        base, frac = head.split(".", 1)
        head = f"{base}.{frac[:6]}"
    try:
        return datetime.fromisoformat(f"{head}+{tz or '00:00'}")
    except ValueError:
        return None


def our_fills(trades: list[dict[str, Any]], order_ids: set[str]) -> list[dict[str, Any]]:
    """Venue trades where one of our orders was a side, flattened."""
    out = []
    for t in trades:
        passive = (t.get("passiveExecution") or {}).get("order") or t.get("passive") or {}
        aggressor = (t.get("aggressorExecution") or {}).get("order") or t.get("aggressor") or {}
        ours = passive if str(passive.get("id")) in order_ids else (
            aggressor if str(aggressor.get("id")) in order_ids else None)
        if ours is None:
            continue
        out.append({
            "trade_id": str(t.get("id") or ""),
            "slug": str(t.get("marketSlug") or ours.get("marketSlug") or ""),
            "time": str(t.get("createTime") or ""),
            "qty": float(t.get("qtyDecimal") or t.get("qty") or 0.0),
            "price": (t.get("price") or {}).get("value"),
            "order_id": str(ours.get("id")),
            "we_were": "passive" if ours is passive else "aggressor",
            "aggressor_intent": str(aggressor.get("intent") or ""),
            "aggressor_side": str(aggressor.get("side") or ""),
            "aggressor_tif": str(aggressor.get("tif") or ""),
        })
    return out


def audit(
    fills: list[dict[str, Any]],
    raw: list[dict[str, Any]],
    counted_ids: set[str],
    gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify each real fill by what the feed and the model made of it."""
    by_id: dict[str, dict[str, Any]] = {}
    by_slug: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
    for rec in raw:
        tr = parse_trade(rec)
        if tr is None:
            continue
        info = {"px": tr.price, "qty": tr.qty, "taker": tr.taker_side,
                "conn": rec.get("_conn"), "rx": rec.get("_rx"),
                "raw_side": str(((rec.get("trade") or {}).get("taker") or {}).get("side") or "")}
        if tr.trade_id:
            by_id.setdefault(tr.trade_id, info)
        when = _t(tr.trade_time)
        if when is not None:
            by_slug[tr.slug].append((when, info))
    spans = [(_t(str(g.get("start") or "")), _t(str(g.get("end") or ""))) for g in gaps]

    rows = []
    for f in fills:
        when = _t(f["time"])
        feed = by_id.get(f["trade_id"])
        if f["trade_id"] in counted_ids:
            verdict = "counted"
        elif feed is not None:
            verdict = "on feed, not counted"
        else:
            near = [i for w, i in by_slug.get(f["slug"], [])
                    if when is not None and abs(w - when) <= NEAR]
            if near:
                verdict, feed = "on feed, other id", near[0]
            else:
                down = when is not None and any(
                    a is not None and b is not None and a <= when <= b for a, b in spans)
                verdict = "not on feed (in gap)" if down else "not on feed"
        rows.append({**f, "verdict": verdict, "feed": feed})
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "fills": len(rows),
        "verdicts": dict(Counter(r["verdict"] for r in rows)),
        "by_aggressor": dict(Counter((r["aggressor_intent"], r["verdict"]) for r in rows)),
        "feed_sides_seen": dict(Counter(
            (r["feed"] or {}).get("raw_side", "") for r in rows if r["feed"])),
    }
