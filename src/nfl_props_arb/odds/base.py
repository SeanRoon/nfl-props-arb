"""Odds provider interface.

FanDuel has no public API, so the scraper is the fragile part of this system.
Everything downstream depends only on this protocol, which means a broken
scraper degrades the tool to manual entry instead of killing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..edge import american_to_prob
from ..props import GameKey, PropType, Scope


@dataclass(frozen=True)
class PropQuote:
    """One price for one prop, at one scope.

    Carries exactly one of two kinds of price:

    * `american` -- a book's YES price. The NO floor is its complement.
    * `max_buy`  -- an operator's already-fee-adjusted NO limit. There is no
      implied probability to read off it until a market's theta is known, so
      `implied_yes` is None and the floor is resolved in `scan._floor_for_prop`.
    """

    game: GameKey
    prop: PropType
    scope: Scope
    fetched_at: datetime
    american: int | None = None
    max_buy: float | None = None
    team: str | None = None
    label: str = ""      # the book's own wording, kept for operator verification
    source: str = ""

    @property
    def implied_yes(self) -> float | None:
        """Vig-inclusive implied probability of the YES outcome.

        None for a `max_buy` quote: a fee-adjusted buy limit is not a probability,
        and inventing one would put a fabricated number in the report.
        """
        if self.american is None:
            return None
        return american_to_prob(self.american)

    @property
    def no_complement(self) -> float | None:
        implied = self.implied_yes
        return None if implied is None else 1.0 - implied

    def __str__(self) -> str:
        where = f" [{self.team.upper()}]" if self.team else ""
        if self.american is None:
            return f"{self.prop.value}{where} max buy {self.max_buy:.4f} (fee-adjusted)"
        sign = "+" if self.american > 0 else ""
        implied = self.implied_yes
        assert implied is not None
        return f"{self.prop.value}{where} {sign}{self.american} ({implied:.2%})"


@runtime_checkable
class OddsProvider(Protocol):
    """Anything that can supply FanDuel-equivalent prop prices."""

    name: str

    def quotes_for(self, games: list[GameKey]) -> list[PropQuote]:
        """Return every prop quote this provider has for the given games."""
        ...
