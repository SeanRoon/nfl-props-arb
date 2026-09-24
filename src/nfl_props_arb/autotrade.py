"""Turn a scan into orders, under a per-market cap, on unstarted games only.

Deliberately deterministic: the same scan, ledger and clock produce the same
orders every time. Nothing here samples, ranks by anything but price, or exercises
judgement -- the judgement is the operator's `max_buy` numbers, and this module's
job is to obey them exactly.

Four guards, in the order they bite:

1. **Halt file.** `data/HALT` stops the run before anything else happens. A kill
   switch that works by touching a file beats one that needs a process found.
2. **Tradeable.** A prop with no operator limit is scanned and reported but never
   bought. `overtime` is in that state today.
3. **Kickoff.** Checked at planning *and* re-checked immediately before each
   submission, because the scan that produced the prices is minutes old by then.
4. **Cap.** Enforced against the ledger, so it holds across runs rather than
   within one. See `ledger` for why that file is the source of truth.

The cap is measured in **all-in cost**, price plus taker fee, because that is what
actually leaves the account. A $100 cap means at most $100 spent on that market,
not $100 of shares with fees on top.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import ledger
from .baselines import Baseline
from .edge import effective_cost
from .execution.base import OrderClient, OrderRequest, OrderResult
from .props import PropType
from .scan import Opportunity, ScanResult

DEFAULT_MAX_PER_MARKET = 100.0
DEFAULT_KICKOFF_BUFFER = timedelta(minutes=15)
HALT_FILE = Path("data/HALT")

# Polymarket US quotes size to two decimals; anything finer cannot be sent.
SHARE_PRECISION = 2

# Smallest order worth sending (operator, 2026-09-24). The venue accepts 0.01, but
# fees round to the cent per fill, so a sub-share order at a zero-edge limit pays
# away more than it can earn -- and the books are full of 0.01-share dust.
MIN_SHARES = 1.0

# NO prices are derived as `1 - yes_bid`, which leaves float noise: a 0.67 level
# arrives as 0.6699999999999999. Rounding here keeps that out of the limit price
# and out of the ledger. Four places is far finer than the venue's tick, so it
# cannot round a qualifying price up past the operator's limit.
PRICE_PRECISION = 4


@dataclass(frozen=True)
class PlannedOrder:
    """One order this run intends to place."""

    slug: str
    market_id: str
    game: str
    prop: str
    question: str
    price: float
    shares: float
    theta: float
    edge_pts: float
    kickoff: str

    @property
    def all_in_cost(self) -> float:
        """What leaves the account: price plus taker fee, times size."""
        return effective_cost(self.price, self.theta) * self.shares

    @property
    def notional(self) -> float:
        return self.price * self.shares

    @property
    def expected_value(self) -> float:
        return self.edge_pts / 100.0 * self.shares


@dataclass
class AutotradeResult:
    """What one run planned, placed and refused."""

    run_id: str
    planned: list[PlannedOrder] = field(default_factory=list)
    placed: list[tuple[PlannedOrder, OrderResult]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    halted: bool = False

    @property
    def total_cost(self) -> float:
        return sum(p.all_in_cost for p in self.planned)

    @property
    def total_ev(self) -> float:
        return sum(p.expected_value for p in self.planned)


def _floor_shares(shares: float) -> float:
    """Round size DOWN to a sendable precision.

    Down, never nearest: rounding up would breach the cap by a hair, and a cap
    that is approximately enforced is not a cap.
    """
    factor: int = 10**SHARE_PRECISION
    return float(math.floor(shares * factor)) / factor


def parse_kickoff(raw: str) -> datetime | None:
    """Parse a kickoff string to an aware UTC datetime, or None if it will not."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def has_started(kickoff: str, now: datetime, buffer: timedelta) -> bool:
    """Whether a kickoff string is in the past, imminent, or unreadable.

    Unreadable counts as started, for the same reason as `PmProp.has_started`:
    the check exists to rule out trading a live game, and a kickoff that cannot
    be established is precisely the case where it cannot be ruled out.
    """
    parsed = parse_kickoff(kickoff)
    if parsed is None:
        return True
    return parsed - buffer <= now


def is_halted(halt_file: Path | str = HALT_FILE) -> bool:
    return Path(halt_file).exists()


def plan_orders(
    result: ScanResult,
    thresholds: dict[PropType, Baseline],
    *,
    exposure: dict[str, float],
    cap: float = DEFAULT_MAX_PER_MARKET,
    now: datetime | None = None,
    kickoff_buffer: timedelta = DEFAULT_KICKOFF_BUFFER,
) -> tuple[list[PlannedOrder], list[dict[str, Any]]]:
    """Decide what to buy. Pure: no network, no disk, no clock of its own."""
    moment = now or result.generated_at
    planned: list[PlannedOrder] = []
    skipped: list[dict[str, Any]] = []

    for opp in result.opportunities:
        if not opp.triggered:
            continue
        reason = _rejection(opp, thresholds, moment, kickoff_buffer)
        if reason is not None:
            skipped.append({"slug": opp.prop.slug, "reason": reason})
            continue

        slug = opp.prop.slug
        spent = exposure.get(slug, 0.0)
        remaining = cap - spent
        if remaining <= 0.0:
            skipped.append({"slug": slug, "reason": "cap_reached", "exposure": spent})
            continue

        # Edges arrive sorted by descending edge, which for a fixed floor is the
        # same as ascending price: the cheapest shares are taken first.
        took_any = False
        for edge in opp.edges:
            per_share = effective_cost(edge.no_price, opp.prop.theta)
            affordable = remaining / per_share
            if affordable < MIN_SHARES:
                break  # the cap cannot fund a minimum order at any deeper price
            shares = _floor_shares(min(edge.qty, affordable))
            if shares < MIN_SHARES:
                # A dust level. A deeper order still sweeps it first, being cheaper.
                continue
            order = PlannedOrder(
                slug=slug,
                market_id=opp.prop.market_id,
                game=str(opp.prop.key.game),
                prop=opp.prop.key.prop.value,
                question=opp.prop.question,
                price=round(edge.no_price, PRICE_PRECISION),
                shares=shares,
                theta=opp.prop.theta,
                edge_pts=edge.edge_pts,
                kickoff=opp.prop.start_date,
            )
            planned.append(order)
            took_any = True
            remaining -= order.all_in_cost

        if not took_any:
            skipped.append(
                {"slug": slug, "reason": "size_below_minimum", "remaining": remaining}
            )

    return planned, skipped


def _rejection(
    opp: Opportunity,
    thresholds: dict[PropType, Baseline],
    now: datetime,
    kickoff_buffer: timedelta,
) -> str | None:
    """Why this opportunity must not be traded, or None if it may be."""
    base = thresholds.get(opp.prop.key.prop)
    if base is None or not base.tradeable:
        return "not_tradeable"
    if base.max_buy is None:
        # Reporting off an assumed book price is fine; buying off one is not.
        return "no_operator_limit"
    if opp.prop.has_started(now, kickoff_buffer):
        return "started"
    if opp.best is None:
        return "no_qualifying_level"
    if opp.best.no_price > base.max_buy + 1e-9:
        # Belt and braces: the scan already applied this, but a mistake in the
        # threshold plumbing must not be allowed to reach the venue.
        return "above_max_buy"
    return None


def execute(
    planned: list[PlannedOrder],
    client: OrderClient,
    *,
    run_id: str,
    ledger_path: Path | str = ledger.DEFAULT_PATH,
    now: datetime | None = None,
    kickoff_buffer: timedelta = DEFAULT_KICKOFF_BUFFER,
) -> list[tuple[PlannedOrder, OrderResult]]:
    """Place each planned order, recording the intent before it is sent.

    The kickoff re-check is not redundant with `plan_orders`. Discovery, pricing
    and book fetches take real time against a rate-limited gateway, so by the
    time the last order of a slate is sent the plan can be minutes old -- long
    enough for a 1pm game to have kicked off.
    """
    out: list[tuple[PlannedOrder, OrderResult]] = []
    for order in planned:
        moment = now or datetime.now(UTC)
        if has_started(order.kickoff, moment, kickoff_buffer):
            out.append(
                (
                    order,
                    OrderResult(
                        status=ledger.REJECTED,
                        message=f"kickoff passed before submission ({order.kickoff})",
                    ),
                )
            )
            continue

        key = ledger.idempotency_key(run_id, order.slug, order.price, order.shares)
        request = OrderRequest(
            slug=order.slug,
            market_id=order.market_id,
            side="NO",
            limit_price=order.price,
            shares=order.shares,
            idempotency_key=key,
        )
        ledger.append(_record(order, run_id, key, "intent", ""), ledger_path)
        try:
            outcome = client.place(request)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            # The intent stays on the books. An exception is not a rejection:
            # the order may have reached the venue, so the capital stays counted
            # until the operator reconciles it.
            ledger.append(
                _record(order, run_id, key, "intent", f"submission raised: {exc!r}"),
                ledger_path,
            )
            raise
        # An IOC order that fills in part is cancelled for the rest, so a fill
        # commits only what it took, costed at our own limit price.
        committed = None
        extra: dict[str, Any] = {}
        if outcome.status in ledger.FILL_STATUSES:
            committed = effective_cost(order.price, order.theta) * outcome.filled_shares
            extra = {"filled_shares": outcome.filled_shares}
            if outcome.avg_price is not None:
                # The venue quotes avgPx long-side even on NO orders.
                extra["fill_no_price"] = round(1.0 - outcome.avg_price, PRICE_PRECISION)
        ledger.append(
            _record(
                order, run_id, key, outcome.status, outcome.message, outcome.order_id,
                notional=committed, extra=extra,
            ),
            ledger_path,
        )
        out.append((order, outcome))
    return out


def fit_to_budget(
    planned: list[PlannedOrder], budget: float
) -> tuple[list[PlannedOrder], list[dict[str, Any]]]:
    """Keep planned orders, in order, while their all-in cost fits `budget`.

    The order that crosses the line is shrunk to what is left; everything after
    it is dropped. The venue would refuse them anyway, but refusing here keeps
    the ledger free of orders that never stood a chance.
    """
    kept: list[PlannedOrder] = []
    skipped: list[dict[str, Any]] = []
    remaining = budget
    for order in planned:
        if order.all_in_cost <= remaining:
            kept.append(order)
            remaining -= order.all_in_cost
            continue
        shares = _floor_shares(remaining / effective_cost(order.price, order.theta))
        if shares >= MIN_SHARES:
            shrunk = replace(order, shares=shares)
            kept.append(shrunk)
            remaining -= shrunk.all_in_cost
        else:
            skipped.append(
                {"slug": order.slug, "reason": "buying_power", "remaining": remaining}
            )
    return kept, skipped


def _record(
    order: PlannedOrder,
    run_id: str,
    key: str,
    status: str,
    note: str,
    order_id: str | None = None,
    *,
    notional: float | None = None,
    extra: dict[str, Any] | None = None,
) -> ledger.OrderRecord:
    return ledger.OrderRecord(
        ts=ledger.now_iso(),
        run_id=run_id,
        slug=order.slug,
        market_id=order.market_id,
        game=order.game,
        prop=order.prop,
        side="NO",
        price=order.price,
        shares=order.shares,
        notional=order.all_in_cost if notional is None else notional,
        status=status,
        idempotency_key=key,
        order_id=order_id,
        note=note,
        extra=extra or {},
    )
