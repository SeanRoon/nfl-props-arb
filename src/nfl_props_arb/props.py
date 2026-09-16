"""Prop taxonomy and the Polymarket US market-type mapping.

Polymarket US tags every market with an explicit ``sportsMarketType`` enum, so
classifying the Polymarket side needs no text parsing at all -- an exact lookup
replaces what would otherwise be fragile regex. Scope (whole-game vs one team)
is encoded in the enum name: ``football_game_*`` vs ``football_team_*``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PropType(StrEnum):
    DST_TD = "dst_td"          # any defensive or special-teams touchdown
    PICK_SIX = "pick_six"      # interception returned for a touchdown
    RETURN_TD = "return_td"    # kickoff or punt returned for a touchdown
    SAFETY = "safety"
    OVERTIME = "overtime"
    TWO_PT = "two_pt"          # successful two-point conversion


class Scope(StrEnum):
    GAME = "game"   # "either team" / whole game
    TEAM = "team"   # one specific team


# Exact map from Polymarket's sportsMarketType -> (prop, scope).
# Verified live 2026-09-16 against 128 open markets across 16 games.
PM_MARKET_TYPES: dict[str, tuple[PropType, Scope]] = {
    "football_game_total_defensive_special_teams_touchdowns": (PropType.DST_TD, Scope.GAME),
    "football_team_total_defensive_special_teams_touchdowns": (PropType.DST_TD, Scope.TEAM),
    "football_game_pick_six": (PropType.PICK_SIX, Scope.GAME),
    "football_game_kickoff_punt_return_touchdown": (PropType.RETURN_TD, Scope.GAME),
    "football_game_safety": (PropType.SAFETY, Scope.GAME),
    "football_game_overtime": (PropType.OVERTIME, Scope.GAME),
    "football_game_total_two_point_conversions": (PropType.TWO_PT, Scope.GAME),
}

# Every target market is an "Over 0.5" line, i.e. "at least one". A 1.5 line
# would mean "at least two" -- a different question that must never be matched
# against a FanDuel "will it happen" price.
REQUIRED_LINE = 0.5

_TICKER_RE = re.compile(
    r"^nfl-(?P<away>[a-z]{2,4})-(?P<home>[a-z]{2,4})-(?P<date>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True, order=True)
class GameKey:
    """Canonical identifier for one NFL game: AWAY@HOME on a kickoff date."""

    away: str
    home: str
    date: str  # ISO date of kickoff, as Polymarket labels it

    def __str__(self) -> str:
        return f"{self.away.upper()}@{self.home.upper()} {self.date}"

    @classmethod
    def from_ticker(cls, ticker: str) -> GameKey | None:
        """Parse a Polymarket event ticker like ``nfl-det-buf-2026-09-17``.

        Polymarket lists NFL with away-team-first ordering (``ordering: "away"``
        in /v1/sports), so the first abbreviation is the away team.
        """
        m = _TICKER_RE.match(ticker.strip().lower())
        if not m:
            return None
        return cls(away=m["away"], home=m["home"], date=m["date"])

    @property
    def teams(self) -> tuple[str, str]:
        return (self.away, self.home)


@dataclass(frozen=True)
class PropKey:
    """Join key: a prop on a game, optionally narrowed to one team."""

    game: GameKey
    prop: PropType
    scope: Scope
    team: str | None = None  # set iff scope is TEAM

    def __post_init__(self) -> None:
        if self.scope is Scope.TEAM and not self.team:
            raise ValueError("TEAM scope requires a team abbreviation")
        if self.scope is Scope.GAME and self.team:
            raise ValueError("GAME scope must not carry a team")

    def __str__(self) -> str:
        where = f" [{self.team.upper()}]" if self.team else ""
        return f"{self.game} {self.prop.value}{where}"

    @property
    def as_game(self) -> PropKey:
        """The whole-game key this (possibly team-scoped) key rolls up into."""
        return PropKey(game=self.game, prop=self.prop, scope=Scope.GAME)
