"""The long-running side of the LIVE 1-cent extremes trader: clock, network, disk.

`extremes_live.py` decides; this module streams, places and logs. Three feeds:

* the **market stream** (book snapshots and trades), as in the paper trader;
* the **private order stream** (`/v1/ws/private`), the only source of real
  fills, acks and venue cancels -- live runs only;
* a **placement worker** that sends decided bids in batches of 20.

Every bid is logged as an `intent` *before* it is sent, and a key with an
intent is never sent again, even after a restart. An ambiguous reply (409, 5xx,
a lost connection) is therefore never retried: a second copy of a bid is a
second share at risk, while a missing one only costs a paper/live comparison.

Dry run (the default) runs everything except the HTTP call: each bid is "acked"
on the spot, so the shadow model runs exactly as it would live, and nothing
fills for real. It needs credentials only for the market stream.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .extremes_live import (
    DEFAULT_LIVE_SHARES,
    DEFAULT_MAX_EXPOSURE,
    DEFAULT_SKIP,
    Intent,
    LiveTracker,
)
from .extremes_paper import (
    DISCOVER_EVERY,
    STATUS_EVERY,
    append_log,
    discover_markets,
    raw_path,
    raw_records,
    read_log,
)

LIVE_LOG = Path("data/extremes/live.jsonl")
DRY_LOG = Path("data/extremes/live-dryrun.jsonl")
BATCHES_PER_SECOND = 4.0   # 80 orders/s; the venue allows 25 requests/s shared
BREAKER_FAILURES = 3       # consecutive refused/ambiguous batches before placing stops


def reconcile_path(log_path: Path | str) -> Path:
    """Order states fetched by `extremes-live-reconcile`, kept out of the trader's log."""
    p = Path(log_path)
    return p.with_name(p.stem + "-reconcile.jsonl")


def read_live_records(log_path: Path | str) -> list[dict[str, Any]]:
    return read_log(log_path) + read_log(reconcile_path(log_path))


def dry_ack(order_id: str) -> dict[str, Any]:
    """What the private stream would say when a bid comes to rest."""
    return {"orderSubscriptionUpdate": {"execution": {
        "id": f"{order_id}-new", "type": "EXECUTION_TYPE_NEW",
        "order": {"id": order_id, "state": "ORDER_STATE_NEW", "cumQuantity": "0"}}}}


async def run(
    *,
    live: bool,
    hours: float,
    shares: float = DEFAULT_LIVE_SHARES,
    skip: frozenset[tuple[str, str]] = DEFAULT_SKIP,
    max_exposure: float = DEFAULT_MAX_EXPOSURE,
    max_hours: float = 40.0,
    groups_per_conn: int = 5,
    log_path: Path | None = None,
    game: str | None = None,
    quotas: dict[tuple[str, str], int] | None = None,
    allowed_keys: set[tuple[str, str]] | None = None,
    halted: Callable[[], bool] = lambda: False,
    say: Callable[[str], None] = print,
) -> LiveTracker:
    """Stream, place and shadow until every market finishes, `max_hours`, or HALT.

    `game` keeps only events whose ticker contains it (e.g. `cin-was`), for a
    small test on one game.
    """
    from .execution.client import LiveOrderClient, RestingBid
    from .execution.credentials import load as load_credentials
    from .execution.stream import (
        MAX_GROUPS_PER_CONN,
        PRIVATE_PATH,
        chunks,
        private_subscriptions,
        run_connection,
    )

    log = log_path or (LIVE_LOG if live else DRY_LOG)
    credentials = load_credentials()
    per_conn = max(1, min(groups_per_conn, MAX_GROUPS_PER_CONN))
    tracker = LiveTracker(shares=shares, skip=skip, max_exposure=max_exposure, quotas=quotas,
                          allowed_keys=allowed_keys)
    prior = read_live_records(log)
    tracker.replay(prior)
    if prior:
        s = tracker.summary()
        say(f"replayed {len(prior)} records: {s['placed']:g} placed, "
            f"exposure ${s['exposure']:.2f}")
        last = max((str(r.get("ts") or "") for r in prior), default="")
        append_log([{"event": "gap", "ts": datetime.now(UTC).isoformat(), "conn": -1,
                     "reason": "restart", "start": last,
                     "end": datetime.now(UTC).isoformat()}], log)

    client = LiveOrderClient(credentials) if live else None
    if client is not None:
        power = await asyncio.to_thread(client.buying_power)
        say(f"buying power ${power:,.2f}; exposure cap ${max_exposure:,.2f}")
        if power < max_exposure:
            say("WARNING: buying power is under the cap. The venue cancels resting bids "
                "it can no longer fund once fills spend the cash; those shadows stop too.")

    market_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100_000)
    private_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100_000)
    intents_q: asyncio.Queue[Intent] = asyncio.Queue()
    stop = asyncio.Event()
    placing = asyncio.Event()
    placing.set()
    tasks: list[asyncio.Task[None]] = []
    deadline = datetime.now(UTC) + timedelta(hours=max_hours)

    async def placer() -> None:
        from .execution.client import MAX_BATCH

        failures = 0
        while not stop.is_set():
            try:
                first = await asyncio.wait_for(intents_q.get(), timeout=1.0)
            except TimeoutError:
                continue
            batch = [first]
            while not intents_q.empty() and len(batch) < MAX_BATCH:
                batch.append(intents_q.get_nowait())
            if not placing.is_set() or halted():
                continue  # dropped before logging an intent: never sent
            append_log([tracker.intent_record(i, datetime.now(UTC)) for i in batch], log)
            recs: list[dict[str, Any]] = []
            if client is None:
                for i in batch:
                    oid = f"dry-{i.slug}-{i.side}"
                    now = datetime.now(UTC)
                    recs += tracker.placed(i, oid, now)
                    recs += tracker.handle_private(dry_ack(oid), now)
            else:
                wire = [RestingBid(i.slug, i.side, i.long_price, i.shares, i.expires_at)
                        for i in batch]
                try:
                    result = await asyncio.to_thread(client.place_resting_batch, wire)
                    status, ids, message = result.status, result.order_ids, result.message
                except Exception as exc:  # noqa: BLE001 - may have been accepted
                    status, ids, message = "unknown", [], f"request raised: {exc!r}"
                now = datetime.now(UTC)
                if status == "accepted":
                    failures = 0
                    for i, oid in zip(batch, ids, strict=True):
                        recs += tracker.placed(i, oid, now)
                else:
                    failures += 1
                    for n, i in enumerate(batch):
                        oid = ids[n] if n < len(ids) else ""
                        recs += tracker.failed(i, status, message, now, order_id=oid)
                    say(f"batch {status}: {message[:300]}")
                    if failures >= BREAKER_FAILURES:
                        placing.clear()
                        say(f"{failures} batches in a row refused or ambiguous: placing "
                            "STOPPED for this run. Streaming continues for what rests.")
            append_log(recs, log)
            await asyncio.sleep(1.0 / BATCHES_PER_SECOND)

    async def discover() -> None:
        metas = await asyncio.to_thread(discover_markets, hours)
        if game:
            metas = [m for m in metas if game.lower() in m.event_ticker.lower()]
        new = tracker.add_markets(metas)
        groups = list(chunks(new))
        for i in range(0, len(groups), per_conn):
            tasks.append(asyncio.create_task(run_connection(
                len(tasks), credentials, groups[i : i + per_conn], market_q, stop)))
        say(f"discovered {len(metas)} markets; {len(new)} new, {len(tasks)} tasks")

    if client is not None:
        # Before any bid goes out, so no ack can be missed.
        tasks.append(asyncio.create_task(run_connection(
            -1, credentials, [], private_q, stop,
            path=PRIVATE_PATH, subscriptions=private_subscriptions())))
    tasks.append(asyncio.create_task(placer()))
    await discover()
    if not tracker.metas:
        say("no NFL game markets in the window; nothing to do")
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        return tracker

    last_discover = last_status = datetime.now(UTC)
    private_seen = 0
    errors_shown = 0
    raw_file = raw_path(log, last_discover)
    say(f"raw stream capture -> {raw_file}")
    try:
        while True:
            now = datetime.now(UTC)
            if halted():
                say("HALT file present: stopping. Resting bids stay up until their GTD "
                    "expiry; `nflprops extremes-live-cancel --live` pulls them now.")
                break
            if now >= deadline:
                say(f"max runtime {max_hours:g}h reached, stopping")
                break
            if not tracker.open_slugs():
                say("every tracked market has finished, stopping")
                break
            if now - last_discover >= DISCOVER_EVERY:
                last_discover = now
                try:
                    await discover()
                except Exception as exc:  # noqa: BLE001 - keep streaming
                    say(f"rediscovery failed, will retry: {exc!r}"[:300])
            if now - last_status >= STATUS_EVERY:
                last_status = now
                s = tracker.summary()
                say(f"{now:%H:%M:%S}Z placed={s['placed']:g} resting={s['resting']:g} "
                    f"rejected={s['rejected']:g} unknown={s['unknown']:g} "
                    f"fills real={s['real_fills']:g} shadow={s['shadow_fills']:g} "
                    f"exposure=${s['exposure']:.2f} private_msgs={private_seen}")

            batch: list[dict[str, Any]] = []
            while not private_q.empty():
                m = private_q.get_nowait()
                private_seen += 1
                stamp = datetime.now(UTC)
                append_log(raw_records([{**m, "_stream": "private"}], stamp), raw_file)
                if "_gap" in m:
                    batch.append({"event": "gap", "ts": stamp.isoformat(), "stream": "private",
                                  **m["_gap"]})
                    say(f"private stream back after gap {m['_gap']}; "
                        "run extremes-live-reconcile to recover fills missed meanwhile")
                    continue
                if "_conn_error" in m:
                    say(f"private stream lost: {m['_conn_error']}")
                    continue
                if private_seen <= 3:
                    say(f"private stream: {json.dumps(m)[:300]}")
                for r in tracker.handle_private(m, stamp):
                    if r["event"] == "order" and r["cum_qty"] > 0:
                        say(f"REAL FILL {r['side']} {r['slug']} cum={r['cum_qty']:g} "
                            f"{r['state']}")
                    batch.append(r)
            try:
                msg = await asyncio.wait_for(market_q.get(), timeout=0.5)
                pending = [msg]
            except TimeoutError:
                pending = []
            while not market_q.empty() and len(pending) < 5000:
                pending.append(market_q.get_nowait())
            stamp = datetime.now(UTC)
            append_log(raw_records(pending, stamp), raw_file)
            for m in pending:
                if "_gap" in m:
                    batch.append({"event": "gap", "ts": stamp.isoformat(), **m["_gap"]})
                    continue
                if "_conn_error" in m or "error" in m or "code" in m:
                    if errors_shown < 10:
                        errors_shown += 1
                        say(f"stream message not handled: {json.dumps(m)[:300]}")
                    continue
                recs, intents = tracker.handle_market(m, stamp)
                for r in recs:
                    if r["event"] == "trade":
                        say(f"SHADOW HIT {r['side']} {r['slug']} px={r['px']} qty={r['qty']} "
                            f"queue={r['queue_filled']:g} ahead={r['ahead']:g}")
                batch.extend(recs)
                for i in intents:
                    intents_q.put_nowait(i)
            append_log(batch, log)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            client.close()
    return tracker


def reconcile(
    log_path: Path, client: Any, say: Callable[[str], None] = print,
    per_second: float = 10.0,
) -> int:
    """Ask the venue for every placed order not known to be finished.

    Recovers fills and cancels the private stream missed (a disconnect, or the
    trader not running). Writes to the reconcile file, never the trader's log,
    so it is safe while the trader runs. Returns the number of orders updated.
    """
    import time

    tracker = LiveTracker()
    tracker.replay(read_live_records(log_path))
    final = ("ORDER_STATE_FILLED",)
    todo = [o for o in tracker.orders.values()
            if o.order_id and o.status in ("placed", "unknown")
            and o.state not in final and not any(w in o.state for w in ("CANCEL", "EXPIRE",
                                                                         "REJECT"))]
    say(f"{len(todo)} order(s) to check")
    out_path = reconcile_path(log_path)
    pending: list[dict[str, Any]] = []
    changed = 0
    for n, o in enumerate(todo, 1):
        try:
            order = client.get_order(o.order_id)
        except Exception as exc:  # noqa: BLE001 - skip and carry on
            say(f"  {o.order_id}: {exc!r}"[:200])
            continue
        now = datetime.now(UTC)
        if order is None:
            pending.append({"event": "order", "ts": now.isoformat(), "slug": o.slug,
                            "side": o.side, "order_id": o.order_id,
                            "state": "ORDER_STATE_REJECTED_NOT_FOUND", "cum_qty": o.cum_qty,
                            "exec_id": "", "source": "reconcile"})
            changed += 1
        else:
            recs = tracker._apply_order((o.slug, o.side), order, now)
            for r in recs:
                r["source"] = "reconcile"
            changed += sum(r["event"] == "order" for r in recs)
            pending.extend(r for r in recs if r["event"] == "order")
        if len(pending) >= 50:
            append_log(pending, out_path)
            pending = []
        if n % 500 == 0:
            say(f"  {n}/{len(todo)} checked")
        time.sleep(1.0 / per_second)
    append_log(pending, out_path)
    say(f"{changed} order(s) updated -> {out_path}")
    return changed
