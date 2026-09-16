"""FanDuel odds provider (undocumented endpoints).

FanDuel publishes no documented public API. The endpoints used here are the ones
its own web client calls. They are unstable by nature, scraping them is contrary
to FanDuel's terms of service, and access is actively defended.

As of 2026-09-16 from this machine, ``sbapi.fanduel.com`` refuses the TLS
handshake outright (``SSLV3_ALERT_HANDSHAKE_FAILURE``) from three independent
TLS stacks: Python ``ssl``, curl/schannel, and curl_cffi/BoringSSL with browser
impersonation. That is edge-level blocking rather than a fingerprint mismatch,
so this provider cannot currently fetch from here. It is written to work on a
network that permits it, and otherwise raises `FanDuelUnavailable` pointing at
manual entry.

The parsing below follows the known shape of FanDuel's ``event-page`` payload
but has **not** been validated against a live response. Treat
`parse_event_page` as unverified until it has run against real data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..props import GameKey, PropType, Scope
from ..teams import find_teams, resolve
from .base import PropQuote

SBAPI = "https://sbapi.fanduel.com/api"
WEB_API_KEY = "FhMFpcPWXMeyZxOx"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Authentication": WEB_API_KEY,
    "Referer": "https://sportsbook.fanduel.com/",
    "Origin": "https://sportsbook.fanduel.com",
}

# Ordered most-specific first. A pick six and a return touchdown are both
# defensive/special-teams scores, so the narrow patterns must match before the
# general D/ST rule is reached.
_TD = r"(?:touchdown|\bTDs?\b)"

_RULES: list[tuple[PropType, re.Pattern[str]]] = [
    (
        PropType.PICK_SIX,
        re.compile(rf"pick[\s\-]?(six|6)|interception\s+return(ed)?\s+for(\s+a)?\s+{_TD}", re.I),
    ),
    (
        PropType.RETURN_TD,
        re.compile(rf"(kick[\s\-]?off|kick|punt)[^.]*return[^.]*{_TD}", re.I),
    ),
    (
        PropType.TWO_PT,
        re.compile(r"(two|2)[\s\-]?point\s+conversion", re.I),
    ),
    (PropType.SAFETY, re.compile(r"\bsafety\b", re.I)),
    (PropType.OVERTIME, re.compile(r"\bovertime\b|\bgo\s+to\s+ot\b", re.I)),
    (
        PropType.DST_TD,
        re.compile(
            rf"(defen[sc]\w*|special\s+teams|d\s*/\s*st)[^.]*{_TD}"
            rf"|{_TD}[^.]*(defen[sc]\w*|special\s+teams)",
            re.I,
        ),
    ),
]


class FanDuelUnavailable(RuntimeError):
    """FanDuel could not be reached, or returned something unusable."""


def classify_market_name(name: str) -> PropType | None:
    """Map a FanDuel market title to a prop type, or None if it is not one of ours."""
    text = (name or "").strip()
    if not text:
        return None
    for prop, pattern in _RULES:
        if pattern.search(text):
            return prop
    return None


def _american(runner: dict[str, Any]) -> int | None:
    """Pull American odds out of a runner, tolerating shape drift."""
    odds = runner.get("winRunnerOdds") or {}
    disp = odds.get("americanDisplayOdds") or {}
    for source in (disp, odds):
        for key in ("americanOddsInt", "americanOdds"):
            val = source.get(key)
            if val is None:
                continue
            try:
                return int(str(val).replace("+", "").strip())
            except ValueError:
                continue
    return None


def parse_event_page(payload: dict[str, Any], game: GameKey) -> list[PropQuote]:
    """Extract our six props from a FanDuel ``event-page`` response.

    Handles both shapes a book may use: a Yes/No market whose title names the
    team, and a single market whose runners are the teams themselves.

    NO-side runners are skipped deliberately: the floor is always derived from
    the YES price so that FanDuel's vig stays on our side of the trade.
    """
    markets = (payload.get("attachments") or {}).get("markets") or {}
    now = datetime.now(UTC)
    out: list[PropQuote] = []
    for mkt in markets.values():
        if not isinstance(mkt, dict):
            continue
        name = mkt.get("marketName") or mkt.get("marketType") or ""
        prop = classify_market_name(name)
        if prop is None:
            continue
        named = find_teams(name)
        market_team = named[0] if len(named) == 1 else None
        for runner in mkt.get("runners") or []:
            if not isinstance(runner, dict):
                continue
            rname = (runner.get("runnerName") or "").strip()
            american = _american(runner)
            if american is None:
                continue
            low = rname.lower()
            if low in {"no", "neither", "none"}:
                continue
            team = market_team
            if low != "yes":
                resolved = resolve(rname)
                if resolved is None or resolved not in game.teams:
                    continue
                team = resolved
            if team is not None and team not in game.teams:
                continue
            out.append(
                PropQuote(
                    game=game,
                    prop=prop,
                    scope=Scope.TEAM if team else Scope.GAME,
                    team=team,
                    american=american,
                    fetched_at=now,
                    label=f"{name} / {rname}".strip(" /"),
                    source="fanduel",
                )
            )
    return out


class FanDuelOdds:
    """Fetches prop prices from FanDuel's web endpoints."""

    name = "fanduel"

    def __init__(self, timeout: float = 25.0, impersonate: str = "chrome124") -> None:
        self.timeout = timeout
        self.impersonate = impersonate

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            from curl_cffi import requests as cr

            fetch: Callable[[], Any] = lambda: cr.get(  # noqa: E731
                url,
                params=params,
                headers=BROWSER_HEADERS,
                impersonate=self.impersonate,
                timeout=self.timeout,
            )
        except ImportError:
            import httpx

            fetch = lambda: httpx.get(  # noqa: E731
                url, params=params, headers=BROWSER_HEADERS, timeout=self.timeout
            )
        try:
            r = fetch()
        except Exception as exc:  # noqa: BLE001 - re-raised with guidance
            raise FanDuelUnavailable(
                f"FanDuel request failed ({type(exc).__name__}). This host blocks "
                "automated clients at the TLS layer. Use --odds-source manual "
                "after running `nflprops odds-template`."
            ) from exc
        if r.status_code != 200:
            raise FanDuelUnavailable(f"FanDuel returned HTTP {r.status_code}")
        try:
            return dict(r.json())
        except Exception as exc:  # noqa: BLE001
            raise FanDuelUnavailable("FanDuel returned a non-JSON body") from exc

    def event_ids(self) -> dict[frozenset[str], str]:
        """Map each NFL game's team pair to FanDuel's event id."""
        payload = self._get(
            f"{SBAPI}/content-managed-page",
            {
                "page": "CUSTOM",
                "customPageId": "nfl",
                "pbHorizonId": 3,
                "timezone": "America/New_York",
                "_ak": WEB_API_KEY,
            },
        )
        events = (payload.get("attachments") or {}).get("events") or {}
        out: dict[frozenset[str], str] = {}
        for eid, ev in events.items():
            if not isinstance(ev, dict):
                continue
            teams = find_teams(ev.get("name") or "")
            if len(teams) == 2:
                out[frozenset(teams)] = str(eid)
        return out

    def quotes_for(self, games: list[GameKey]) -> list[PropQuote]:
        index = self.event_ids()
        out: list[PropQuote] = []
        for game in games:
            eid = index.get(frozenset(game.teams))
            if eid is None:
                continue
            payload = self._get(f"{SBAPI}/event-page", {"eventId": eid, "_ak": WEB_API_KEY})
            out.extend(parse_event_page(payload, game))
        return out
