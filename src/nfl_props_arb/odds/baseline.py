"""Odds provider backed by per-prop baseline assumptions.

Stands in for a live book feed. Emits one quote per game for game-scoped props,
and one per team for team-scoped ones, so the whole-game floor is still built by
the usual leg-product path rather than a special case.

A `max_buy` baseline is different in kind and is emitted differently. It is a flat
fee-adjusted price cap, not a book's read on one team, so it is emitted at *every*
scope the prop can appear at -- whole-game and each team alike. That gives every
market a direct same-scope quote, which is what bypasses the leg-product
derivation: multiplying two price caps together would be meaningless.
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
                for scope, team in self._targets(game, base):
                    out.append(
                        PropQuote(
                            game=game,
                            prop=prop,
                            scope=scope,
                            team=team,
                            american=base.american,
                            max_buy=base.max_buy,
                            fetched_at=now,
                            label=f"baseline {prop.value}",
                            source="baseline",
                        )
                    )
        return out

    @staticmethod
    def _targets(game: GameKey, base: Baseline) -> list[tuple[Scope, str | None]]:
        """Which (scope, team) slots this baseline supplies a quote for."""
        if base.max_buy is not None:
            # Flat cap: quote the whole-game market and every team market.
            return [(Scope.GAME, None), *((Scope.TEAM, t) for t in game.teams)]
        if base.scope is Scope.TEAM:
            return [(Scope.TEAM, t) for t in game.teams]
        return [(Scope.GAME, None)]
