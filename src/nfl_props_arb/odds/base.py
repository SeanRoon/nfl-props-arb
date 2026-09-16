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
    """One sportsbook price for one prop, at one scope."""

    game: GameKey
    prop: PropType
    scope: Scope
    american: int
    fetched_at: datetime
    team: str | None = None
    label: str = ""      # the book's own wording, kept for operator verification
    source: str = ""

    @property
    def implied_yes(self) -> float:
        """Vig-inclusive implied probability of the YES outcome."""
        return american_to_prob(self.american)

    @property
    def no_complement(self) -> float:
        return 1.0 - self.implied_yes

    def __str__(self) -> str:
        where = f" [{self.team.upper()}]" if self.team else ""
        sign = "+" if self.american > 0 else ""
        return f"{self.prop.value}{where} {sign}{self.american} ({self.implied_yes:.2%})"


@runtime_checkable
class OddsProvider(Protocol):
    """Anything that can supply FanDuel-equivalent prop prices."""

    name: str

    def quotes_for(self, games: list[GameKey]) -> list[PropQuote]:
        """Return every prop quote this provider has for the given games."""
        ...
