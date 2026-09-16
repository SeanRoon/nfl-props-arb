"""Core edge math. Pure functions, no I/O, no network.

The strategy in one line: buy NO on Polymarket whenever it costs less than
FanDuel's own implied NO price, using FanDuel's vig as the safety margin.

Why the complement is a *floor*: a sportsbook's quoted YES price is inflated by
vig, so the true YES probability is at most the quoted implied probability.
Therefore true NO >= 1 - implied_yes. Buying NO below that complement means
buying something worth at least what you paid, before fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Polymarket US taker fee: shares * theta * p * (1 - p). Makers pay nothing.
# Each market carries its own coefficient; NFL props observed at 0.06 (2026-09).
DEFAULT_THETA = 0.06


def american_to_prob(odds: int | float) -> float:
    """Convert American odds to vig-inclusive implied probability.

    +650 -> 0.13333 (underdog), -200 -> 0.66667 (favorite).
    """
    odds = float(odds)
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def prob_to_american(p: float) -> int:
    """Inverse of american_to_prob, rounded. For display only."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability out of range: {p}")
    if p <= 0.5:
        return round((1.0 - p) / p * 100.0)
    return -round(p / (1.0 - p) * 100.0)


def fee_per_share(price: float, theta: float = DEFAULT_THETA) -> float:
    """Polymarket US taker fee per share at `price`.

    Symmetric around 0.5: a fill at 0.30 costs the same as one at 0.70.
    """
    return theta * price * (1.0 - price)


def effective_cost(price: float, theta: float = DEFAULT_THETA) -> float:
    """All-in cost per share: quoted price plus taker fee."""
    return price + fee_per_share(price, theta)


def max_buy_price(floor: float, theta: float = DEFAULT_THETA, slippage: float = 0.0) -> float:
    """Highest NO price that still clears the floor once fees are paid.

    Solves ``p + theta*p*(1-p) == floor - slippage`` for p, taking the lower root.

    This replaces a flat percentage buffer. At theta=0.06 a flat 0.5% buffer is
    too small: against a floor of 0.86667 the real break-even is 0.85942, so
    buying at 0.862 is slightly -EV.
    """
    target = floor - slippage
    if target <= 0.0:
        return 0.0
    if theta == 0.0:
        return min(target, 1.0)
    a = theta
    b = -(1.0 + theta)
    c = target
    disc = b * b - 4.0 * a * c
    if disc < 0.0:
        # Fee curve never reaches the target; no price qualifies.
        return 0.0
    root = (-b - math.sqrt(disc)) / (2.0 * a)
    return max(0.0, min(root, 1.0))


@dataclass(frozen=True)
class NoLevel:
    """One resting NO offer, derived from a YES bid."""

    price: float       # NO price = 1 - yes_bid_price
    qty: float         # shares available at this price
    yes_bid: float     # the YES bid this was derived from

    @property
    def notional(self) -> float:
        return self.price * self.qty


def no_levels_from_yes_bids(bids: list[tuple[float, float]]) -> list[NoLevel]:
    """Convert a YES-side bid ladder into the NO offers you can actually buy.

    A resting YES bid at 0.35 *is* a resting NO offer at 0.65 -- buying NO
    crosses against YES bids. Verified against live Polymarket US books, where
    ``shortQuote == 1 - bestBid`` held for all 126 markets with a two-sided book.

    Returned cheapest-NO first, which is the highest YES bid.
    """
    levels = [NoLevel(price=1.0 - px, qty=qty, yes_bid=px) for px, qty in bids if qty > 0]
    return sorted(levels, key=lambda lv: lv.price)


def qualifying_levels(levels: list[NoLevel], max_price: float) -> list[NoLevel]:
    """Every NO offer at or below `max_price`.

    Deliberately unfiltered by size: a single share is reported. Thin size is
    shown to the operator rather than used to suppress the signal.
    """
    return [lv for lv in levels if lv.price <= max_price + 1e-9]


@dataclass(frozen=True)
class Edge:
    """Result of pricing one NO offer against a FanDuel-derived floor."""

    no_price: float
    floor: float
    theta: float
    qty: float

    @property
    def fee(self) -> float:
        return fee_per_share(self.no_price, self.theta)

    @property
    def all_in(self) -> float:
        return effective_cost(self.no_price, self.theta)

    @property
    def edge_pts(self) -> float:
        """Probability points of edge, after fees. Positive means +EV."""
        return (self.floor - self.all_in) * 100.0

    @property
    def ev_per_share(self) -> float:
        return self.floor - self.all_in

    @property
    def roi(self) -> float:
        """Return on deployed capital (all-in cost), as a fraction."""
        return self.ev_per_share / self.all_in if self.all_in > 0 else 0.0

    @property
    def is_positive(self) -> bool:
        return self.ev_per_share > 0.0
