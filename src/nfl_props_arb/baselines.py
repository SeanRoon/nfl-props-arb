"""Price thresholds for each prop, used when no live book feed is available.

Two kinds of threshold live here, and the difference matters.

**Assumed book odds** (`american`). These six props price within a narrow band
across games -- a defensive/special teams touchdown is priced much the same
whoever is playing -- so a per-prop baseline is a serviceable stand-in for a live
feed while one is being sorted out. They are assumptions, not quotes: every row
derived from one is labelled as such, and the report prints the book price each
market would need (`required_american_odds`) so a single manual check settles it.

Direction of error matters. `floor = 1 - implied_yes`, so assuming a *shorter*
price (a smaller + number) implies a higher YES probability, a lower floor, and
therefore a stricter buy threshold. Baselines here deliberately err short: being
wrong costs missed signals rather than bad fills.

**Fee-adjusted buy limits** (`max_buy`). An operator-supplied NO price that is the
most worth paying all-in, fees included. This is what `autotrade` runs on: the
operator has already done the judgement and the fee arithmetic, and the tool's job
is to obey the number rather than re-derive it. Running a `max_buy` through the
fee solver would subtract the fee twice; see `Baseline` and `scan._floor_for_prop`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .props import PropType, Scope

DEFAULT_PATH = Path("data/baselines.toml")
AUTOTRADE_PATH = Path("data/autotrade.toml")


@dataclass(frozen=True)
class Baseline:
    """A price threshold for one prop, plus the edge needed to report it.

    Exactly one of `american` or `max_buy` is set.

    `american` is an assumed *book* price for the YES side; the floor is its
    complement, and the buy threshold is then solved for fees.

    `max_buy` is a NO price that is **already fee-adjusted** -- the highest price
    worth paying, all-in. It is not a floor and must not be run through
    `max_buy_price()` a second time. The floor it implies is
    `effective_cost(max_buy, theta)`, which round-trips exactly back to `max_buy`.

    `tradeable` gates order placement only. A prop with no threshold the operator
    is willing to trade still gets scanned and reported; it just never gets bought.
    """

    scope: Scope
    american: int | None = None
    max_buy: float | None = None
    min_edge_pts: float = 0.0
    tradeable: bool = False

    def __post_init__(self) -> None:
        if (self.american is None) == (self.max_buy is None):
            raise ValueError("set exactly one of 'american' or 'max_buy'")
        if self.american is not None and self.american == 0:
            raise ValueError("american odds cannot be zero")
        if self.max_buy is not None and not 0.0 < self.max_buy < 1.0:
            raise ValueError(f"max_buy must be strictly between 0 and 1, got {self.max_buy}")


# Short-side estimates of typical US retail prices.
#
# All six set by the operator on 2026-09-17.
#
# Caveat worth keeping: Bovada priced DET@BUF overtime at +900 on 2026-09-16,
# so the +1200 here is LONGER than an actually observed quote. A too-long
# baseline overstates the floor and can manufacture edge, which is the failure
# direction that costs money rather than missing trades.
DEFAULT_BASELINES: dict[PropType, Baseline] = {
    PropType.DST_TD: Baseline(american=800, scope=Scope.TEAM, min_edge_pts=2.0),
    PropType.PICK_SIX: Baseline(american=800, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.RETURN_TD: Baseline(american=1100, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.SAFETY: Baseline(american=1100, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.OVERTIME: Baseline(american=1200, scope=Scope.GAME, min_edge_pts=2.0),
    PropType.TWO_PT: Baseline(american=400, scope=Scope.GAME, min_edge_pts=2.0),
}

# Operator-set, fee-adjusted buy limits for `nflprops autotrade`, 2026-09-22.
#
# D/ST TD is deliberately scope="game" here, unlike DEFAULT_BASELINES: 0.69 is a
# whole-game number, so the leg-product derivation is bypassed and the same flat
# limit applies to team-scoped D/ST markets too. In practice a single team's NO
# rarely trades under 0.69, so this trades the whole-game market in the main.
#
# OVERTIME has no operator limit, so it stays untradeable: it is still scanned
# and reported off the assumed baseline, and never bought.
DEFAULT_AUTOTRADE: dict[PropType, Baseline] = {
    PropType.DST_TD: Baseline(max_buy=0.69, scope=Scope.GAME, tradeable=True),
    PropType.TWO_PT: Baseline(max_buy=0.69, scope=Scope.GAME, tradeable=True),
    PropType.PICK_SIX: Baseline(max_buy=0.83, scope=Scope.GAME, tradeable=True),
    PropType.RETURN_TD: Baseline(max_buy=0.83, scope=Scope.GAME, tradeable=True),
    PropType.SAFETY: Baseline(max_buy=0.83, scope=Scope.GAME, tradeable=True),
    PropType.OVERTIME: Baseline(american=1200, scope=Scope.GAME, tradeable=False),
}

TEMPLATE = """# Baseline sportsbook odds for each prop, used by --odds-source baseline.
#
# These stand in for a live book feed. They are ASSUMPTIONS: the scan prints the
# price each market would actually need ("Book need"), so verify the top edges
# against a sportsbook before trading.
#
# Set exactly one of `american` or `max_buy` per prop.
#
#   american     - assumed YES price at the book. Err SHORT (smaller +number): a
#                  shorter assumed price lowers the floor and makes the scan
#                  stricter. The buy threshold is then solved for fees.
#   max_buy      - a NO price you have ALREADY fee-adjusted: the most you will
#                  pay all-in. Used as-is, never re-solved. An offer at exactly
#                  this price qualifies, at zero edge.
#   scope        - "team" if the book prices it per team (the whole-game floor is
#                  then the product of both legs), otherwise "game".
#   min_edge_pts - suppress rows below this edge, in probability points.
#   tradeable    - may `autotrade` buy this prop? Defaults to false.

[dst_td]
american = 800
scope = "team"
min_edge_pts = 2.0

[pick_six]
american = 800
scope = "game"
min_edge_pts = 2.0

[return_td]
american = 1100
scope = "game"
min_edge_pts = 2.0

[safety]
american = 1100
scope = "game"
min_edge_pts = 2.0

[overtime]
american = 1200
scope = "game"
min_edge_pts = 2.0

[two_pt]
american = 400
scope = "game"
min_edge_pts = 2.0
"""

AUTOTRADE_TEMPLATE = """# Fee-adjusted buy limits for `nflprops autotrade`.
#
# Kept separate from baselines.toml on purpose: the scanner's ASSUMPTIONS and the
# trader's LIVE LIMITS must never drift into one another.
#
#   max_buy   - the most you will pay for a NO share, all-in, fees included.
#               Used exactly as written: the tool does not re-subtract fees. An
#               offer at exactly this price qualifies, at zero edge.
#   scope     - "game" applies one flat limit to the whole-game market and to
#               team-scoped markets alike, bypassing the leg-product derivation.
#   tradeable - false leaves the prop scanned and reported but never bought.
#
# A prop with `american` instead of `max_buy` is priced the scanner's way: floor
# from the complement, threshold solved for fees.

[dst_td]
max_buy = 0.69
scope = "game"
tradeable = true

[two_pt]
max_buy = 0.69
scope = "game"
tradeable = true

[pick_six]
max_buy = 0.83
scope = "game"
tradeable = true

[return_td]
max_buy = 0.83
scope = "game"
tradeable = true

[safety]
max_buy = 0.83
scope = "game"
tradeable = true

# No operator limit set for overtime, so it is never bought.
[overtime]
american = 1200
scope = "game"
tradeable = false
"""


def write_template(path: Path | str = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def write_autotrade_template(path: Path | str = AUTOTRADE_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AUTOTRADE_TEMPLATE, encoding="utf-8")
    return path


def load(
    path: Path | str = DEFAULT_PATH,
    defaults: dict[PropType, Baseline] | None = None,
) -> dict[PropType, Baseline]:
    """Load thresholds from TOML, falling back to `defaults`.

    Each table sets exactly one of `american` or `max_buy`; setting both, or
    neither, is an error rather than a silent precedence rule.
    """
    path = Path(path)
    out = dict(DEFAULT_BASELINES if defaults is None else defaults)
    if not path.exists():
        return out
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for name, cfg in raw.items():
        try:
            prop = PropType(name.strip().lower())
        except ValueError as exc:
            raise ValueError(f"{path}: unknown prop '{name}'") from exc
        has_american = "american" in cfg
        has_max_buy = "max_buy" in cfg
        if has_american == has_max_buy:
            raise ValueError(
                f"{path}: [{name}] must set exactly one of 'american' or 'max_buy'"
            )
        current = out.get(prop)
        try:
            out[prop] = Baseline(
                scope=Scope(str(cfg.get("scope", "game")).lower()),
                american=int(cfg["american"]) if has_american else None,
                max_buy=float(cfg["max_buy"]) if has_max_buy else None,
                min_edge_pts=float(
                    cfg.get("min_edge_pts", current.min_edge_pts if current else 0.0)
                ),
                tradeable=bool(cfg.get("tradeable", False)),
            )
        except ValueError as exc:
            raise ValueError(f"{path}: [{name}] {exc}") from exc
    return out


def apply_overrides(
    baselines: dict[PropType, Baseline], overrides: list[str]
) -> dict[PropType, Baseline]:
    """Apply CLI overrides of the form ``dst_td=+850`` or ``dst_td=0.69``.

    A value with a decimal point is a fee-adjusted `max_buy`; anything else is
    American odds. `0.69` and `+850` are unambiguous on sight, which is the point:
    an operator typing one should never silently get the other.
    """
    out = dict(baselines)
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"override must look like prop=+850 or prop=0.69, got {item!r}")
        name, value = item.split("=", 1)
        try:
            prop = PropType(name.strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown prop '{name.strip()}'") from exc
        current = out.get(prop, DEFAULT_BASELINES[prop])
        raw = value.strip()
        try:
            if "." in raw:
                out[prop] = Baseline(
                    scope=current.scope,
                    max_buy=float(raw),
                    min_edge_pts=current.min_edge_pts,
                    tradeable=current.tradeable,
                )
            else:
                out[prop] = Baseline(
                    scope=current.scope,
                    american=int(raw.replace("+", "")),
                    min_edge_pts=current.min_edge_pts,
                    tradeable=current.tradeable,
                )
        except ValueError as exc:
            raise ValueError(f"bad override {item!r}: {exc}") from exc
    return out
