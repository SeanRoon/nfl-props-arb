"""Read-only client for the Polymarket US public gateway.

Market data on Polymarket US needs no authentication and no KYC: the public
gateway serves markets, books, BBO, events and sports. Only order placement and
portfolio access require API keys, and this package contains no such code.

Polymarket US is a separate CFTC-regulated exchange from international
Polymarket, with its own order book and its own liquidity. Prices must come from
this host or the quotes are not fillable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..edge import NoLevel, no_levels_from_yes_bids
from ..props import PM_MARKET_TYPES, REQUIRED_LINE, GameKey, PropKey, Scope

GATEWAY = "https://gateway.polymarket.us"


@dataclass
class PmProp:
    """One Polymarket US prop market we know how to price."""

    key: PropKey
    slug: str
    market_id: str
    question: str
    title: str
    theta: float
    line: float
    event_ticker: str
    start_date: str
    best_yes_bid: float | None = None   # NO ask == 1 - best_yes_bid
    levels: list[NoLevel] = field(default_factory=list)

    @property
    def no_ask(self) -> float | None:
        """Cheapest NO available, i.e. the top of the derived NO ladder."""
        if self.best_yes_bid is None:
            return None
        return 1.0 - self.best_yes_bid

    @property
    def url(self) -> str:
        return f"https://polymarket.us/market/{self.slug}"


def _amount(node: Any) -> float | None:
    if not node:
        return None
    try:
        return float(node["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _team_from_slug(slug: str, game: GameKey) -> str | None:
    """Recover the team a team-scoped market belongs to.

    Slugs look like ``astatc-nfl-det-buf-2026-09-17-tdstd-det-0pt5``; the team
    code appears as its own segment after the prop code. Matching against the
    game's own two teams avoids being fooled by the away-home pair earlier in
    the slug.
    """
    segments = slug.lower().split("-")
    tail = segments[segments.index("tdstd") + 1 :] if "tdstd" in segments else segments
    for seg in tail:
        if seg in game.teams:
            return seg
    return None


class PolymarketUS:
    """Thin read client. One HTTP connection, reused."""

    def __init__(self, timeout: float = 30.0, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=GATEWAY,
            timeout=timeout,
            headers={"User-Agent": "nfl-props-arb/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PolymarketUS:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        """GET with a short backoff.

        Public gateway endpoints are rate limited (about 60 requests/minute), and
        a slate scan is request-heavy, so transient 429s and connection resets
        are retried rather than failing the whole run.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self._client.get(path, params=clean)
                if r.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                payload: dict[str, Any] = json.loads(r.content.decode("utf-8"))
                return payload
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                if attempt == 3:
                    break
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"Polymarket request failed after retries: {path}") from last

    def nfl_events(self, start_min: str, start_max: str, limit: int = 200) -> list[dict[str, Any]]:
        """Open NFL events with kickoff inside the window (ISO-8601 UTC strings)."""
        data = self._get(
            "/v1/events",
            tagSlug="nfl",
            closed="false",
            limit=limit,
            orderBy="startDate",
            orderDirection="asc",
            startDateMin=start_min,
            startDateMax=start_max,
        )
        return data.get("events") or []

    def props_from_events(
        self, events: list[dict[str, Any]]
    ) -> tuple[list[PmProp], list[dict[str, Any]]]:
        """Extract the prop markets we can price, plus anything skipped.

        Skips are returned rather than dropped so gaps in coverage stay visible.
        """
        props: list[PmProp] = []
        skipped: list[dict[str, Any]] = []
        for ev in events:
            game = GameKey.from_ticker(ev.get("ticker") or "")
            if game is None:
                continue  # futures / division winners, not a game
            for m in ev.get("markets") or []:
                smt = m.get("sportsMarketType")
                mapping = PM_MARKET_TYPES.get(smt or "")
                if mapping is None:
                    continue
                prop, scope = mapping
                line = m.get("line")
                if line is None or abs(float(line) - REQUIRED_LINE) > 1e-9:
                    # e.g. an Over 1.5 line means "at least two" -- different question.
                    skipped.append({"slug": m.get("slug"), "reason": "line", "line": line})
                    continue
                team = None
                if scope is Scope.TEAM:
                    team = _team_from_slug(m.get("slug") or "", game)
                    if team is None:
                        skipped.append({"slug": m.get("slug"), "reason": "team_unresolved"})
                        continue
                props.append(
                    PmProp(
                        key=PropKey(game=game, prop=prop, scope=scope, team=team),
                        slug=m["slug"],
                        market_id=str(m.get("id")),
                        question=m.get("question") or "",
                        title=m.get("title") or "",
                        theta=float(m.get("feeCoefficient") or 0.0),
                        line=float(line),
                        event_ticker=ev.get("ticker") or "",
                        start_date=ev.get("startDate") or "",
                        best_yes_bid=_amount(m.get("bestBidQuote")),
                    )
                )
        return props, skipped

    def load_book(self, prop: PmProp) -> PmProp:
        """Populate the NO ladder from the market's YES bid side.

        The book is quoted in YES terms. A resting YES bid at 0.35 is a resting
        NO offer at 0.65, so the NO ladder is the YES bids mirrored.
        """
        data = self._get(f"/v1/markets/{prop.slug}/book").get("marketData") or {}
        bids = [
            (float(b["px"]["value"]), float(b["qty"]))
            for b in (data.get("bids") or [])
            if _amount(b.get("px")) is not None
        ]
        prop.levels = no_levels_from_yes_bids(bids)
        if bids:
            prop.best_yes_bid = max(px for px, _ in bids)
        return prop


def discover(
    client: PolymarketUS, start_min: str, start_max: str
) -> tuple[list[PmProp], list[dict[str, Any]]]:
    """Convenience: events -> priceable props for a kickoff window."""
    return client.props_from_events(client.nfl_events(start_min, start_max))
