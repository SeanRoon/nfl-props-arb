"""Append-only record of every order this tool has intended or placed.

The scanner is stateless: each run rediscovers the slate and prices it fresh.
That is fine when a human reads the output and decides. It is not fine on an
hourly timer, because a market that qualifies at 09:00 still qualifies at 10:00,
and a process with no memory would buy it again, and again, for as long as the
offer stands. Sixteen hourly runs against a $100 cap is $1,600 of exposure.

So the cap is enforced against this file, not against the current run. The rule:

    exposure(slug) = sum of notional for every record that is not rejected

and a new order is sized so that `exposure(slug) + notional <= cap`.

**Written before submission, reconciled after.** An order that is submitted but
whose response is lost must count against the cap -- the money may well be gone.
Recording first and correcting later overstates exposure when a submission fails,
which costs a missed trade. Recording after would understate it when a response
is lost, which costs a double position. Only one of those is acceptable.

JSON lines, one record per line, fsynced. A flat file is deliberate: it stays
readable when something has gone wrong and the operator needs to see what the
machine thought it was doing.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("data/ledger.jsonl")

# Statuses that tie up capital. `rejected` is the only one that does not, and it
# is set only when the venue has affirmatively refused the order.
OPEN_STATUSES = frozenset({"intent", "submitted", "filled", "partial"})
REJECTED = "rejected"
DRY_RUN = "dry_run"


@dataclass(frozen=True)
class OrderRecord:
    """One order, as intended and then as resolved."""

    ts: str
    run_id: str
    slug: str
    market_id: str
    game: str
    prop: str
    side: str            # always "NO" today; recorded so it cannot be assumed later
    price: float
    shares: float
    notional: float
    status: str
    idempotency_key: str
    order_id: str | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


def idempotency_key(run_id: str, slug: str, price: float, shares: float) -> str:
    """Stable id for one intended order.

    Deterministic in the run, so a retry inside the same run resolves to the same
    key and the venue can reject the duplicate. Distinct across runs, because the
    next hour's order at the same price genuinely is a different order.
    """
    raw = f"{run_id}|{slug}|{price:.6f}|{shares:.6f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def read(path: Path | str = DEFAULT_PATH) -> Iterator[OrderRecord]:
    """Yield every record. A malformed line is skipped, not fatal.

    A corrupt tail -- a half-written line from a killed process -- must not stop
    the cap being enforced on everything before it.
    """
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                yield OrderRecord(**raw)
            except (json.JSONDecodeError, TypeError):
                continue


def load_exposure(path: Path | str = DEFAULT_PATH) -> dict[str, float]:
    """Cumulative committed notional per market slug.

    Counts intents and fills; ignores rejections and dry runs. Latest status for
    a given idempotency key wins, so a reconciliation that marks an intent
    rejected releases its capital.
    """
    latest: dict[str, OrderRecord] = {}
    for rec in read(path):
        latest[rec.idempotency_key] = rec
    exposure: dict[str, float] = {}
    for rec in latest.values():
        if rec.status in OPEN_STATUSES:
            exposure[rec.slug] = exposure.get(rec.slug, 0.0) + rec.notional
    return exposure


def append(record: OrderRecord, path: Path | str = DEFAULT_PATH) -> OrderRecord:
    """Append one record and flush it to disk before returning.

    The fsync is the point. This function's whole job is to make sure the
    intention survives the process dying one instruction later.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.to_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
