"""Resting NO bids at the operator's limit, across every market the autotrader targets.

The autotrader *takes*: each hour it buys whatever NO is already offered at or
below `max_buy`. This module *makes*: it leaves a standing bid at `max_buy` in
every target market, so a seller who arrives between hourly runs trades with us.

Separate from the autotrader on purpose. Different order type, different
lifetime, different risk, and the operator runs it by hand.

Three rules, each the answer to a way resting orders go wrong:

1. **Every bid expires before kickoff.** Good-till-date, set to kickoff minus the
   buffer. A resting NO bid that survives into the game is a free option for
   anyone watching it: the moment a two-point try lines up, the bid gets hit at a
   price that is no longer worth paying. Expiry is enforced by the venue, so it
   holds even if this machine is asleep.
2. **Post-only.** A bid that would cross the book is rejected by the venue rather
   than filled as a taker. Taking is the autotrader's job; markets with an offer
   already at or under the limit are skipped here and left to it.
3. **One open order per market.** A market with any open NO order already --
   ours or placed by hand -- is skipped, so repeated runs never stack bids.

Makers pay no fee on Polymarket US, so a maker fill at 0.69 costs exactly 0.69:
already inside a limit that was set with the taker fee included.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .baselines import Baseline
from .polymarket.client import PmProp
from .props import PropType

DEFAULT_LOG = Path("data/bids.jsonl")
DEFAULT_SHARES = 50.0


@dataclass(frozen=True)
class PlannedBid:
    """One resting NO bid to place."""

    slug: str
    game: str
    prop: str
    team: str | None
    no_price: float
    shares: float
    expires_at: datetime
    kickoff: str

    @property
    def notional(self) -> float:
        """Buying power the venue reserves while the bid rests. Makers pay no fee."""
        return self.no_price * self.shares


def plan_bids(
    props: list[PmProp],
    thresholds: dict[PropType, Baseline],
    *,
    shares: float,
    now: datetime,
    kickoff_buffer: timedelta,
    open_slugs: set[str],
    only: set[PropType] | None = None,
    price: float | None = None,
) -> tuple[list[PlannedBid], list[dict[str, Any]]]:
    """Decide which bids to place. Pure: no network, no disk, no clock of its own."""
    planned: list[PlannedBid] = []
    skipped: list[dict[str, Any]] = []

    for prop in sorted(props, key=lambda p: (p.start_date, p.slug)):
        kind = prop.key.prop
        if only is not None and kind not in only:
            continue
        base = thresholds.get(kind)
        if base is None or not base.tradeable or base.max_buy is None:
            skipped.append({"slug": prop.slug, "reason": "not_tradeable"})
            continue
        limit = base.max_buy if price is None else price
        if price is not None and price > base.max_buy + 1e-9:
            # A bid above the operator's limit is a trade the operator said no to.
            skipped.append({"slug": prop.slug, "reason": "above_max_buy"})
            continue
        kickoff = prop.kickoff_at
        if kickoff is None or prop.has_started(now, kickoff_buffer):
            skipped.append({"slug": prop.slug, "reason": "started"})
            continue
        if prop.slug in open_slugs:
            skipped.append({"slug": prop.slug, "reason": "open_order_exists"})
            continue
        no_ask = prop.no_ask
        if no_ask is not None and no_ask <= limit + 1e-9:
            # Post-only would be rejected; the autotrader takes this one instead.
            skipped.append({"slug": prop.slug, "reason": "would_cross", "no_ask": no_ask})
            continue
        planned.append(
            PlannedBid(
                slug=prop.slug,
                game=str(prop.key.game),
                prop=kind.value,
                team=prop.key.team,
                no_price=round(limit, 4),
                shares=shares,
                expires_at=kickoff - kickoff_buffer,
                kickoff=prop.start_date,
            )
        )
    return planned, skipped


def fit_to_budget(
    planned: list[PlannedBid], budget: float
) -> tuple[list[PlannedBid], list[dict[str, Any]]]:
    """Keep whole bids, soonest kickoff first, while their reservation fits.

    Bids are not shrunk to fit: a 50-share order was asked for, and a scatter of
    odd sizes is harder to read in the app than a clear "these did not fit".
    """
    kept: list[PlannedBid] = []
    skipped: list[dict[str, Any]] = []
    remaining = budget
    for bid in planned:
        if bid.notional <= remaining + 1e-9:
            kept.append(bid)
            remaining -= bid.notional
        else:
            skipped.append({"slug": bid.slug, "reason": "buying_power"})
    return kept, skipped


def log(entry: dict[str, Any], path: Path | str = DEFAULT_LOG) -> None:
    """Append one record, fsynced. The order ids here are what `bids-cancel` targets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def placed_order_ids(path: Path | str = DEFAULT_LOG) -> set[str]:
    """Every order id this tool has placed. A malformed line is skipped."""
    path = Path(path)
    ids: set[str] = set()
    if not path.exists():
        return ids
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "placed" and rec.get("order_id"):
                ids.add(rec["order_id"])
    return ids


def record(bid: PlannedBid, event: str, **fields: Any) -> dict[str, Any]:
    return {"ts": datetime.now(UTC).isoformat(), "event": event, **asdict(bid), **fields}
