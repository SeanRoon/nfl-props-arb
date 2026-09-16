"""The scan pipeline: Polymarket props + FanDuel quotes -> priced opportunities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .combine import Floor, IncompleteLegsError, Leg, direct_floor, leg_product_floor
from .edge import Edge, NoLevel, max_buy_price, qualifying_levels
from .odds.base import OddsProvider, PropQuote
from .polymarket.client import PmProp, PolymarketUS, discover
from .props import GameKey, PropType, Scope

# (game, prop, scope, team) -- the exact join key between the two venues.
QuoteKey = tuple[GameKey, PropType, Scope, str | None]

# Extra cushion beyond the modelled taker fee, for slippage and staleness.
DEFAULT_SLIPPAGE = 0.0


@dataclass
class Opportunity:
    """One Polymarket prop priced against a FanDuel-derived floor."""

    prop: PmProp
    floor: Floor
    max_buy: float
    edges: list[Edge] = field(default_factory=list)
    levels: list[NoLevel] = field(default_factory=list)
    quotes: tuple[PropQuote, ...] = ()

    @property
    def triggered(self) -> bool:
        return bool(self.edges)

    @property
    def best(self) -> Edge | None:
        return self.edges[0] if self.edges else None

    @property
    def best_edge_pts(self) -> float:
        return self.best.edge_pts if self.best else float("-inf")

    @property
    def total_shares(self) -> float:
        return sum(e.qty for e in self.edges)

    @property
    def total_cost(self) -> float:
        return sum(e.all_in * e.qty for e in self.edges)

    @property
    def total_ev(self) -> float:
        return sum(e.ev_per_share * e.qty for e in self.edges)

    @property
    def fd_summary(self) -> str:
        return ", ".join(str(q) for q in self.quotes)


@dataclass
class ScanResult:
    opportunities: list[Opportunity]
    considered: int
    unpriced: list[dict[str, Any]]
    generated_at: datetime

    @property
    def triggered(self) -> list[Opportunity]:
        return [o for o in self.opportunities if o.triggered]


def _floor_for_prop(
    prop: PmProp,
    by_key: dict[QuoteKey, PropQuote],
    legs_by_game: dict[tuple[GameKey, PropType], dict[str, Leg]],
) -> tuple[Floor | None, tuple[PropQuote, ...], str | None]:
    """Resolve the NO floor for one Polymarket prop.

    Prefers a direct same-scope quote. For a whole-game market with only
    per-team legs, falls back to the product of the legs.
    """
    key = prop.key
    direct = by_key.get((key.game, key.prop, key.scope, key.team))
    if direct is not None:
        return direct_floor(direct.implied_yes), (direct,), None

    if key.scope is Scope.GAME:
        legs = legs_by_game.get((key.game, key.prop), {})
        if not legs:
            return None, (), "no_fanduel_quote"
        try:
            floor = leg_product_floor(key, legs)
        except IncompleteLegsError as exc:
            return None, (), f"incomplete_legs:{','.join(exc.missing)}"
        leg_quotes = tuple(
            by_key[(key.game, key.prop, Scope.TEAM, t)]
            for t in key.game.teams
            if (key.game, key.prop, Scope.TEAM, t) in by_key
        )
        return floor, leg_quotes, None

    return None, (), "no_fanduel_quote"


def run_scan(
    provider: OddsProvider,
    *,
    days: int = 8,
    slippage: float = DEFAULT_SLIPPAGE,
    client: PolymarketUS | None = None,
) -> ScanResult:
    """Discover props, price them against `provider`, and return opportunities."""
    owns_client = client is None
    client = client or PolymarketUS()
    now = datetime.now(UTC)
    try:
        props, _skipped = discover(
            client,
            now.strftime("%Y-%m-%dT00:00:00Z"),
            (now + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z"),
        )
        games = sorted({p.key.game for p in props})
        quotes = provider.quotes_for(games)

        by_key: dict[QuoteKey, PropQuote] = {
            (q.game, q.prop, q.scope, q.team): q for q in quotes
        }
        legs_by_game: dict[tuple[GameKey, PropType], dict[str, Leg]] = {}
        for q in quotes:
            if q.scope is Scope.TEAM and q.team:
                legs_by_game.setdefault((q.game, q.prop), {})[q.team] = Leg(
                    team=q.team, american=q.american, implied_yes=q.implied_yes
                )

        opportunities: list[Opportunity] = []
        unpriced: list[dict[str, Any]] = []
        for prop in props:
            floor, used, reason = _floor_for_prop(prop, by_key, legs_by_game)
            if floor is None:
                unpriced.append({"key": str(prop.key), "slug": prop.slug, "reason": reason})
                continue
            client.load_book(prop)
            max_buy = max_buy_price(floor.value, prop.theta, slippage)
            levels = qualifying_levels(prop.levels, max_buy)
            edges = [
                Edge(no_price=lv.price, floor=floor.value, theta=prop.theta, qty=lv.qty)
                for lv in levels
            ]
            edges = [e for e in edges if e.is_positive]
            opportunities.append(
                Opportunity(
                    prop=prop,
                    floor=floor,
                    max_buy=max_buy,
                    edges=sorted(edges, key=lambda e: -e.edge_pts),
                    levels=prop.levels,
                    quotes=used,
                )
            )
        opportunities.sort(key=lambda o: -o.best_edge_pts)
        return ScanResult(
            opportunities=opportunities,
            considered=len(props),
            unpriced=unpriced,
            generated_at=now,
        )
    finally:
        if owns_client:
            client.close()
