"""The long-running side of the 1-cent extremes paper trader, plus its log.

Discovers every NFL game market, streams book snapshots and trades for all of
them, and appends to `data/extremes/paper.jsonl` whatever changes a virtual
bid. The fill logic lives in `extremes.py`; this module owns the clock, the
network and the disk. It places nothing: the stream is read-only.

One log across runs, keyed by market and side. A restart replays it, so a
virtual bid keeps the queue position it was given when it first joined, as a
real resting order would.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .extremes import DEFAULT_SHARES, MarketMeta, Tracker, markets_from_events
from .polymarket.client import PolymarketUS

DEFAULT_LOG = Path("data/extremes/paper.jsonl")
DISCOVER_EVERY = timedelta(minutes=30)
STATUS_EVERY = timedelta(minutes=5)
# The whole-slate events payload is ~48 MB; the default 30 s timeout is too short.
DISCOVERY_TIMEOUT = 300.0


def read_log(path: Path | str = DEFAULT_LOG) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_log(records: list[dict[str, Any]], path: Path | str = DEFAULT_LOG) -> None:
    """Append a batch, fsynced once. Batched because a slate's joins run to ~20,000."""
    if not records:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":"), sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def discover_markets(hours: float, now: datetime | None = None) -> list[MarketMeta]:
    """Every open market of every NFL game from yesterday to `hours` ahead.

    Starts a day back so games already under way are included: the whole point
    is to hold bids through play.
    """
    moment = now or datetime.now(UTC)
    lo = (moment - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    hi = (moment + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with PolymarketUS(timeout=DISCOVERY_TIMEOUT) as pm:
        return markets_from_events(pm.nfl_events(lo, hi))


async def run(
    *,
    hours: float,
    shares: float = DEFAULT_SHARES,
    groups_per_conn: int = 5,
    max_hours: float = 40.0,
    log_path: Path = DEFAULT_LOG,
    halted: Callable[[], bool] = lambda: False,
    say: Callable[[str], None] = print,
) -> Tracker:
    """Stream until every tracked market has finished, `max_hours` pass, or HALT."""
    from .execution.credentials import load as load_credentials
    from .execution.stream import MAX_GROUPS_PER_CONN, chunks, run_connection

    credentials = load_credentials()
    per_conn = max(1, min(groups_per_conn, MAX_GROUPS_PER_CONN))
    tracker = Tracker(shares=shares)
    prior = read_log(log_path)
    tracker.replay(prior)
    if prior:
        say(f"replayed {len(prior)} log records, {len(tracker.bids)} virtual bids")
        # Nothing was watched while this process was down.
        last = max((str(r.get("ts") or "") for r in prior), default="")
        append_log(
            [{"event": "gap", "ts": datetime.now(UTC).isoformat(), "conn": -1,
              "reason": "restart", "start": last, "end": datetime.now(UTC).isoformat(),
              "slugs": len({b.slug for b in tracker.bids.values()})}],
            log_path,
        )

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100_000)
    stop = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    started = datetime.now(UTC)
    deadline = started + timedelta(hours=max_hours)

    async def discover() -> None:
        metas = await asyncio.to_thread(discover_markets, hours)
        new = tracker.add_markets(metas)
        groups = list(chunks(new))
        for i in range(0, len(groups), per_conn):
            conn_id = len(tasks)
            tasks.append(
                asyncio.create_task(
                    run_connection(conn_id, credentials, groups[i : i + per_conn], queue, stop)
                )
            )
        games = len({m.event_ticker for m in metas})
        say(f"discovered {len(metas)} markets in {games} games; {len(new)} new, "
            f"{len(tasks)} connections")

    await discover()
    if not tracker.metas:
        say("no NFL game markets in the window; nothing to watch")
        return tracker

    last_discover = last_status = datetime.now(UTC)
    messages = 0
    errors_shown = 0
    try:
        while True:
            now = datetime.now(UTC)
            if halted():
                say("HALT file present, stopping")
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
                say(
                    f"{now:%H:%M:%S}Z msgs={messages} markets={s['markets']:g} "
                    f"bids={s['joined']:g} touched={s['touched']:g} "
                    f"fills opt={s['optimistic_fills']:g} queue={s['queue_fills']:g} "
                    f"finished={s['finished']:g}"
                )

            batch: list[dict[str, Any]] = []
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            pending = [msg]
            while not queue.empty() and len(pending) < 5000:
                pending.append(queue.get_nowait())
            stamp = datetime.now(UTC)
            for m in pending:
                messages += 1
                if "_gap" in m:
                    batch.append({"event": "gap", "ts": stamp.isoformat(), **m["_gap"]})
                    say(f"reconnected after gap: {m['_gap']}")
                    continue
                if "_conn_error" in m:
                    say(f"connection lost: {m['_conn_error']}")
                    continue
                if "error" in m or "code" in m:
                    if errors_shown < 10:
                        errors_shown += 1
                        say(f"stream error: {json.dumps(m)[:300]}")
                    continue
                recs = tracker.handle(m, stamp)
                for r in recs:
                    if r["event"] == "trade":
                        say(f"HIT {r['side']} {r['slug']} px={r['px']} qty={r['qty']} "
                            f"opt={r['optimistic_filled']:g} queue={r['queue_filled']:g} "
                            f"ahead={r['ahead']:g}")
                batch.extend(recs)
            append_log(batch, log_path)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return tracker


def settle_filled(
    records: list[dict[str, Any]], log_path: Path = DEFAULT_LOG
) -> list[dict[str, Any]]:
    """Fetch the settlement of every market with a fill and no `settle` record yet.

    Few requests: fills are rare, and a settled market never needs asking twice.
    """
    from .extremes import bids_from_log, is_terminal, settlements_from_log

    known = settlements_from_log(records)
    wanted = sorted(
        {b.slug for b in bids_from_log(records).values() if b.optimistic_filled > 0} - set(known)
    )
    if not wanted:
        return []
    new = []
    with PolymarketUS() as pm:
        for slug in wanted:
            data = pm.bbo(slug)
            state = str(data.get("state") or "")
            px = (data.get("settlementPx") or {}).get("value")
            if px is None or not is_terminal(state):
                continue
            new.append(
                {"event": "settle", "ts": datetime.now(UTC).isoformat(), "slug": slug,
                 "settlement_px": float(px), "state": state}
            )
    append_log(new, log_path)
    return new
