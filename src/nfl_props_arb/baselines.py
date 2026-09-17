"""Baseline sportsbook odds, used when no live book feed is available.

These six props price within a narrow band across games -- a defensive/special
teams touchdown is priced much the same whoever is playing -- so a per-prop
baseline is a serviceable stand-in for a live feed while one is being sorted
out.

**Baselines are assumptions, not quotes.** Every row derived from one is
labelled as such, and the report prints the book price each market would need
(`required_american_odds`) so a single manual check settles it.

Direction of error matters. `floor = 1 - implied_yes`, so assuming a *shorter*
price (a smaller + number) implies a higher YES probability, a lower floor, and
therefore a stricter buy threshold. Baselines here deliberately err short: being
wrong costs missed signals rather than bad fills.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .props import PropType, Scope

DEFAULT_PATH = Path("data/baselines.toml")


@dataclass(frozen=True)
class Baseline:
    """An assumed book price for one prop, plus the edge needed to report it."""

    american: int
    scope: Scope
    min_edge_pts: float = 0.0


# Short-side estimates of typical US retail prices.
#
# dst_td / pick_six / two_pt were set by the operator on 2026-09-17 and are the
# closest to real lines. return_td and safety remain unmeasured placeholders.
# Overtime is anchored on a real observation: Bovada priced DET@BUF at +900 on
# 2026-09-16, so +800 here is one notch conservative.
DEFAULT_BASELINES: dict[PropType, Baseline] = {
    PropType.DST_TD: Baseline(american=750, scope=Scope.TEAM, min_edge_pts=2.0),
    PropType.PICK_SIX: Baseline(american=650, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.RETURN_TD: Baseline(american=900, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.SAFETY: Baseline(american=800, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.OVERTIME: Baseline(american=800, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.TWO_PT: Baseline(american=250, scope=Scope.GAME, min_edge_pts=2.0),
}

TEMPLATE = '''# Baseline sportsbook odds for each prop, used by --odds-source baseline.
#
# These stand in for a live book feed. They are ASSUMPTIONS: the scanner reports
# the price each market would actually need ("FD need"), so verify the top edges
# against a sportsbook before trading.
#
#   american     - assumed YES price. Err SHORT (smaller +number): a shorter
#                  assumed price lowers the floor and makes the scan stricter.
#   scope        - "team" if the book prices it per team (the whole-game floor is
#                  then the product of both legs), otherwise "game".
#   min_edge_pts - suppress rows below this edge, in probability points.

[dst_td]
american = 750
scope = "team"
min_edge_pts = 2.0

[pick_six]
american = 650
scope = "game"
min_edge_pts = 2.0

[return_td]
american = 900
scope = "game"
min_edge_pts = 2.0

[safety]
american = 800
scope = "game"
min_edge_pts = 2.0

[overtime]
american = 800
scope = "game"
min_edge_pts = 2.0

[two_pt]
american = 250
scope = "game"
min_edge_pts = 2.0
'''


def write_template(path: Path | str = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def load(path: Path | str = DEFAULT_PATH) -> dict[PropType, Baseline]:
    """Load baselines from TOML, falling back to the built-in defaults."""
    path = Path(path)
    if not path.exists():
        return dict(DEFAULT_BASELINES)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    out = dict(DEFAULT_BASELINES)
    for name, cfg in raw.items():
        try:
            prop = PropType(name.strip().lower())
        except ValueError as exc:
            raise ValueError(f"{path}: unknown prop '{name}'") from exc
        american = int(cfg["american"])
        if american == 0:
            raise ValueError(f"{path}: [{name}] american odds cannot be zero")
        out[prop] = Baseline(
            american=american,
            scope=Scope(str(cfg.get("scope", "game")).lower()),
            min_edge_pts=float(cfg.get("min_edge_pts", 0.0)),
        )
    return out


def apply_overrides(
    baselines: dict[PropType, Baseline], overrides: list[str]
) -> dict[PropType, Baseline]:
    """Apply CLI overrides of the form ``dst_td=+850``."""
    out = dict(baselines)
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"override must look like prop=+850, got {item!r}")
        name, value = item.split("=", 1)
        prop = PropType(name.strip().lower())
        current = out.get(prop, DEFAULT_BASELINES[prop])
        out[prop] = Baseline(
            american=int(value.strip().replace("+", "")),
            scope=current.scope,
            min_edge_pts=current.min_edge_pts,
        )
    return out
