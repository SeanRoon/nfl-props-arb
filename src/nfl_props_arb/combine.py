"""Combining FanDuel's per-team legs into a whole-game NO floor.

FanDuel commonly prices these props per team ("Lions D/ST TD", "Bills D/ST TD")
while Polymarket lists a single whole-game market ("either team"). The
whole-game NO outcome is "neither team does it", so the floor is the product of
the per-team NO complements:

    floor(GAME) = product over teams of (1 - implied_yes(team))

Worked example: both teams at 80% NO -> 0.80 * 0.80 = 0.64, so buy the
whole-game NO at or below 0.64 (less fees).

Two properties worth stating plainly:

* **Vig compounds.** Each leg's complement is already deflated by that leg's
  vig, so the product is deflated further. The resulting floor is stricter than
  a single-leg derivation, which errs in the operator's favour.
* **Independence is an approximation.** The two teams' events are positively
  correlated in practice (a sloppy, turnover-heavy game lifts both), so the true
  "neither" probability is somewhat *higher* than the product. That again makes
  the floor conservative. It is not an exact identity and is not treated as one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .edge import effective_cost, fee_per_share
from .props import GameKey, PropKey, Scope


class IncompleteLegsError(ValueError):
    """Raised when a game-level floor cannot be derived from the legs on hand.

    Deriving from a partial leg set would *overstate* the NO floor and
    manufacture an edge that does not exist, so it is refused outright.
    """

    def __init__(self, key: PropKey, missing: tuple[str, ...]) -> None:
        self.key = key
        self.missing = missing
        super().__init__(f"{key}: missing legs for {', '.join(t.upper() for t in missing)}")


@dataclass(frozen=True)
class Leg:
    """One per-team FanDuel quote feeding a whole-game floor."""

    team: str
    american: int
    implied_yes: float

    @property
    def no_complement(self) -> float:
        return 1.0 - self.implied_yes


@dataclass(frozen=True)
class Floor:
    """A NO-side floor with a record of how it was derived."""

    value: float
    method: str              # "direct" | "leg_product" | "max_buy"
    legs: tuple[Leg, ...] = ()
    max_buy: float | None = None   # set only when method == "max_buy"

    @property
    def explanation(self) -> str:
        """Human-auditable arithmetic, e.g. ``0.800 x 0.800 = 0.640``."""
        if self.method == "max_buy":
            assert self.max_buy is not None
            return (
                f"{self.max_buy:.4f} + fee {self.value - self.max_buy:.4f} "
                f"= {self.value:.4f}  (operator limit)"
            )
        if self.method == "direct":
            return f"1 - {1.0 - self.value:.4f} = {self.value:.4f}"
        parts = " x ".join(f"{lg.no_complement:.3f}" for lg in self.legs)
        teams = "/".join(lg.team.upper() for lg in self.legs)
        return f"{parts} = {self.value:.4f}  ({teams})"


def direct_floor(implied_yes: float) -> Floor:
    """Floor from a single quote covering exactly the market being priced."""
    if not 0.0 <= implied_yes <= 1.0:
        raise ValueError(f"implied probability out of range: {implied_yes}")
    return Floor(value=1.0 - implied_yes, method="direct")


def max_buy_floor(max_buy: float, theta: float) -> Floor:
    """Floor implied by an operator's already-fee-adjusted buy limit.

    The operator has said "pay no more than `max_buy`, all-in". Breaking even at
    that price means the true NO value is exactly what it costs to buy there, so
    the implied floor is the all-in cost:

        floor = max_buy + theta * max_buy * (1 - max_buy)

    This inverts `edge.max_buy_price` exactly -- feeding this floor back through
    the solver returns `max_buy` -- which is what lets a fee-adjusted limit reuse
    the whole existing pricing path. The one thing it must never do is go through
    that solver on the way *in*, which would subtract the fee a second time.

    At the limit price itself the edge is exactly zero. That is intended: the
    operator's number is the break-even point they chose, not a profit target.
    """
    if not 0.0 < max_buy < 1.0:
        raise ValueError(f"max_buy out of range: {max_buy}")
    if fee_per_share(max_buy, theta) < 0.0:
        raise ValueError(f"negative fee from theta={theta}")
    return Floor(value=effective_cost(max_buy, theta), method="max_buy", max_buy=max_buy)


def leg_product_floor(key: PropKey, legs: dict[str, Leg]) -> Floor:
    """Whole-game floor as the product of per-team NO complements.

    `legs` is keyed by lowercase team abbreviation. Every team in the game must
    be present or `IncompleteLegsError` is raised -- see the class docstring for
    why a partial derivation is unsafe.
    """
    if key.scope is not Scope.GAME:
        raise ValueError("leg products derive whole-game floors only")
    missing = tuple(t for t in key.game.teams if t not in legs)
    if missing:
        raise IncompleteLegsError(key, missing)
    ordered = tuple(legs[t] for t in key.game.teams)
    value = 1.0
    for leg in ordered:
        value *= leg.no_complement
    return Floor(value=value, method="leg_product", legs=ordered)


def floor_for(key: PropKey, direct: float | None, legs: dict[str, Leg]) -> Floor:
    """Prefer a direct whole-game quote; fall back to the leg product.

    A book that prices the whole-game market itself is strictly better evidence
    than a product built under an independence assumption.
    """
    if direct is not None:
        return direct_floor(direct)
    return leg_product_floor(key, legs)


def game_key_legs(game: GameKey, legs: list[Leg]) -> dict[str, Leg]:
    """Index legs by team, keeping only teams that belong to this game."""
    return {lg.team: lg for lg in legs if lg.team in game.teams}
