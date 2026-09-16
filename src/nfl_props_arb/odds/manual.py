"""Manual odds entry -- the provider that cannot break.

Reads a CSV the operator fills in from the FanDuel app or site. This is the
guaranteed path when scraping is blocked, and it is also the reference format
the scraper emits into, so both providers produce identical downstream data.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from ..props import PM_MARKET_TYPES, GameKey, PropType, Scope
from .base import PropQuote

HEADER = ["game_ticker", "prop", "scope", "team", "american_odds", "label"]

DEFAULT_PATH = Path("data/manual_odds.csv")


def _parse_american(raw: str) -> int:
    text = raw.strip().replace("+", "").replace(" ", "")
    if not text:
        raise ValueError("blank odds")
    return int(text)


class ManualOdds:
    """Load prop quotes from a CSV of hand-entered FanDuel numbers."""

    name = "manual"

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)

    def quotes_for(self, games: list[GameKey]) -> list[PropQuote]:
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} not found -- run `nflprops odds-template` to create it"
            )
        by_ticker = {f"nfl-{g.away}-{g.home}-{g.date}": g for g in games}
        out: list[PropQuote] = []
        now = datetime.now(UTC)
        with self.path.open(encoding="utf-8", newline="") as fh:
            for lineno, row in enumerate(csv.DictReader(fh), start=2):
                raw_odds = (row.get("american_odds") or "").strip()
                if not raw_odds:
                    continue  # unfilled template row
                ticker = (row.get("game_ticker") or "").strip().lower()
                game = by_ticker.get(ticker)
                if game is None:
                    continue  # a game outside the requested slate
                try:
                    american = _parse_american(raw_odds)
                    prop = PropType(row["prop"].strip().lower())
                    scope = Scope(row["scope"].strip().lower())
                except (KeyError, ValueError) as exc:
                    raise ValueError(f"{self.path}:{lineno}: {exc}") from exc
                team = (row.get("team") or "").strip().lower() or None
                if scope is Scope.TEAM and team not in game.teams:
                    raise ValueError(
                        f"{self.path}:{lineno}: team {team!r} is not in {game}"
                    )
                out.append(
                    PropQuote(
                        game=game,
                        prop=prop,
                        scope=scope,
                        team=team if scope is Scope.TEAM else None,
                        american=american,
                        fetched_at=now,
                        label=(row.get("label") or "").strip(),
                        source="manual",
                    )
                )
        return out


def write_template(games: list[GameKey], path: Path | str = DEFAULT_PATH) -> Path:
    """Emit a CSV with one blank row per prop we would know how to price.

    Team-scoped props get one row per team, because FanDuel prices those per
    team and both legs are required before a whole-game floor can be derived.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[list[str]] = []
    for game in sorted(games):
        ticker = f"nfl-{game.away}-{game.home}-{game.date}"
        for prop, scope in sorted({v for v in PM_MARKET_TYPES.values()}, key=lambda x: x[0].value):
            teams = game.teams if scope is Scope.TEAM else (None,)
            for team in teams:
                rows.append([ticker, prop.value, scope.value, team or "", "", ""])
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)
    return path
