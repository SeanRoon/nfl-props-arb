"""Re-verify the Polymarket US invariants this package depends on.

Hits the live public gateway, so it is a script rather than a test (tests are
offline by convention). Run it whenever Polymarket changes something or a
result looks wrong:

    uv run python scripts/verify_invariants.py

It checks the four facts recorded in CLAUDE.md:

1. the side flagged ``long: true`` is always "Yes";
2. the NO quote equals ``1 - bestBid``, i.e. NO offers are mirrored YES bids;
3. ``outcomes`` ordering is genuinely inconsistent, so positional indexing of
   ``outcomePrices`` is unsafe;
4. every target market is an Over 0.5 line.
"""

from __future__ import annotations

import collections
import json
import sys
from datetime import UTC, datetime, timedelta

import httpx

from nfl_props_arb.props import PM_MARKET_TYPES, REQUIRED_LINE

GATEWAY = "https://gateway.polymarket.us"


def fetch_markets(days: int = 8) -> list[dict]:
    now = datetime.now(UTC)
    r = httpx.get(
        f"{GATEWAY}/v1/events",
        params={
            "tagSlug": "nfl",
            "closed": "false",
            "limit": 200,
            "orderBy": "startDate",
            "orderDirection": "asc",
            "startDateMin": now.strftime("%Y-%m-%dT00:00:00Z"),
            "startDateMax": (now + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z"),
        },
        timeout=30,
    )
    r.raise_for_status()
    events = json.loads(r.content.decode("utf-8")).get("events", [])
    return [
        m
        for ev in events
        for m in (ev.get("markets") or [])
        if m.get("sportsMarketType") in PM_MARKET_TYPES
    ]


def main() -> int:
    markets = fetch_markets()
    if not markets:
        print("No target markets in the window (off-season or schedule gap).")
        return 0

    long_bad = no_bad = line_bad = checked = 0
    orders: collections.Counter[tuple[str, ...]] = collections.Counter()

    for m in markets:
        sides = m.get("marketSides") or []
        long_side = next((s for s in sides if s.get("long") is True), None)
        short_side = next((s for s in sides if s.get("long") is False), None)
        if not long_side or not short_side:
            long_bad += 1
            continue
        if long_side.get("description") != "Yes":
            long_bad += 1
        orders[tuple(json.loads(m["outcomes"]))] += 1
        if abs(float(m.get("line") or -1) - REQUIRED_LINE) > 1e-9:
            line_bad += 1
        best_bid = m.get("bestBidQuote")
        if not best_bid:
            continue
        checked += 1
        if abs(float(short_side["quote"]["value"]) - (1 - float(best_bid["value"]))) > 1e-6:
            no_bad += 1

    print(f"markets checked: {len(markets)} ({checked} with a two-sided book)")
    print(f"  1. long-side-is-Yes violations      : {long_bad}")
    print(f"  2. NO == 1 - bestBid violations     : {no_bad}")
    print(f"  3. outcomes orderings seen          : {dict(orders)}")
    print(f"  4. non-0.5 line markets             : {line_bad}")

    failures = long_bad + no_bad + line_bad
    if len(orders) < 2:
        print("\nNOTE: only one outcomes ordering seen this time. Positional")
        print("indexing is still unsafe -- the mixed ordering was observed live.")
    if failures:
        print(f"\nFAILED: {failures} violation(s). CLAUDE.md invariants need revisiting.")
        return 1
    print("\nAll invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
