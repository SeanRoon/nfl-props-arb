"""Odds provider backed by per-prop baseline assumptions.

Stands in for a live book feed. Emits one quote per game for game-scoped props,
and one per team for team-scoped ones, so the whole-game floor is still built by
the usual leg-product path rather than a special case.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..baselines import Baseline, load
from ..props import GameKey, PropType, Scope
from .base import PropQuote


class BaselineOdds:
    """Synthesises quotes from the baseline table."""

    name = "baseline"

    def __init__(self, baselines: dict[PropType, Baseline] | None = None) -> None:
        self.baselines: dict[PropType, Baseline] = (
            baselines if baselines is not None else load()
        )

    def quotes_for(self, games: list[GameKey]) -> list[PropQuote]:
        now = datetime.now(UTC)
        out: list[PropQuote] = []
        for game in games:
            for prop, base in self.baselines.items():
                teams = game.teams if base.scope is Scope.TEAM else (None,)
                for team in teams:
                    out.append(
                        PropQuote(
                            game=game,
                            prop=prop,
                            scope=base.scope,
                            team=team,
                            american=base.american,
                            fetched_at=now,
                            label=f"baseline {prop.value}",
                            source="baseline",
                        )
                    )
        return out
