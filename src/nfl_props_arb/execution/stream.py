"""Authenticated market-data stream from Polymarket US. Read-only: places nothing.

It lives in `execution/` only because the handshake is signed with the same
Ed25519 key as orders, and credentials stay inside this package.

Written to docs.polymarket.us (read 2026-09-30) and a live probe the same day:

  * `wss://api.polymarket.us/v1/ws/markets`. Auth headers are the REST ones,
    signed over `timestamp + "GET" + "/v1/ws/markets"`.
  * Subscribe with `{"subscribe": {"requestId", "subscriptionType",
    "marketSlugs"}}`, at most 100 slugs per subscription.
  * `SUBSCRIPTION_TYPE_MARKET_DATA` sends a **full book snapshot** on every
    change (bids, offers, state, session stats), not deltas.
  * `SUBSCRIPTION_TYPE_TRADE` sends one message per print. `price` is long-side,
    and `taker.side` says who crossed. `quantity` is labelled USD but carries
    shares.
  * **At most 10 subscriptions per connection** (undocumented; found live
    2026-09-30 as `"max subscriptions per connection reached"`). With both
    types per group that is 5 groups, 500 markets, per connection.

**What a disconnect costs:** trades printed while down are gone; the stream does
not replay them. Each reconnect is reported as a gap (start, end, slug count) so
the report can say which windows went unwatched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .client import auth_headers
from .credentials import Credentials

WS_HOST = "wss://api.polymarket.us"
WS_PATH = "/v1/ws/markets"
MAX_SLUGS_PER_SUB = 100
SUB_TYPES = ("SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE")
MAX_SUBS_PER_CONN = 10
MAX_GROUPS_PER_CONN = MAX_SUBS_PER_CONN // len(SUB_TYPES)


def chunks(slugs: list[str], size: int = MAX_SLUGS_PER_SUB) -> Iterator[list[str]]:
    for i in range(0, len(slugs), size):
        yield slugs[i : i + size]


def subscribe_messages(conn_id: int, groups: list[list[str]]) -> list[dict[str, Any]]:
    """Both subscription types for each group of <= 100 slugs."""
    return [
        {
            "subscribe": {
                "requestId": f"c{conn_id}-g{g}-{kind.rsplit('_', 1)[-1].lower()}",
                "subscriptionType": kind,
                "marketSlugs": group,
            }
        }
        for g, group in enumerate(groups)
        for kind in SUB_TYPES
    ]


async def run_connection(
    conn_id: int,
    credentials: Credentials,
    groups: list[list[str]],
    out: asyncio.Queue[dict[str, Any]],
    stop: asyncio.Event,
) -> None:
    """Hold one connection open until `stop`, reconnecting with backoff.

    Every message goes onto `out` as-is. A lost connection puts a `_gap` record
    there once it is back, since nothing seen while down can be recovered.
    """
    import websockets  # the 'execute' extra

    delay = 1.0
    down_since: str | None = None
    n_slugs = sum(len(g) for g in groups)
    while not stop.is_set():
        try:
            headers = auth_headers(credentials, "GET", WS_PATH)
            async with websockets.connect(
                WS_HOST + WS_PATH, additional_headers=headers, max_size=None,
                ping_interval=20, ping_timeout=30, open_timeout=30,
            ) as ws:
                for sub in subscribe_messages(conn_id, groups):
                    await ws.send(json.dumps(sub))
                if down_since is not None:
                    await out.put(
                        {"_gap": {"conn": conn_id, "start": down_since,
                                  "end": datetime.now(UTC).isoformat(), "slugs": n_slugs}}
                    )
                    down_since = None
                delay = 1.0
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except TimeoutError:
                        continue
                    try:
                        await out.put(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means reconnect
            if down_since is None:
                down_since = datetime.now(UTC).isoformat()
                await out.put({"_conn_error": {"conn": conn_id, "error": repr(exc)[:300]}})
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 60.0)
