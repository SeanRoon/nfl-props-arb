"""NFL team identity and alias resolution.

Abbreviations are Polymarket US's own (lowercase, as they appear in event
tickers like ``nfl-det-buf-2026-09-17``), captured from live event payloads on
2026-09-16. Sportsbooks name teams differently, so lookups accept full names,
nicknames, and abbreviations.

Note that Polymarket's ``tagSlug=nfl`` filter leaks a few non-NFL teams (Celtic
FC and a "Tigers" entry were both present), which is why this list is explicit
rather than derived at runtime.
"""

from __future__ import annotations

# abbreviation -> (full name, nickname, city)
TEAMS: dict[str, tuple[str, str, str]] = {
    "ari": ("Arizona Cardinals", "Cardinals", "Arizona"),
    "atl": ("Atlanta Falcons", "Falcons", "Atlanta"),
    "bal": ("Baltimore Ravens", "Ravens", "Baltimore"),
    "buf": ("Buffalo Bills", "Bills", "Buffalo"),
    "car": ("Carolina Panthers", "Panthers", "Carolina"),
    "chi": ("Chicago Bears", "Bears", "Chicago"),
    "cin": ("Cincinnati Bengals", "Bengals", "Cincinnati"),
    "cle": ("Cleveland Browns", "Browns", "Cleveland"),
    "dal": ("Dallas Cowboys", "Cowboys", "Dallas"),
    "den": ("Denver Broncos", "Broncos", "Denver"),
    "det": ("Detroit Lions", "Lions", "Detroit"),
    "gb": ("Green Bay Packers", "Packers", "Green Bay"),
    "hou": ("Houston Texans", "Texans", "Houston"),
    "ind": ("Indianapolis Colts", "Colts", "Indianapolis"),
    "jax": ("Jacksonville Jaguars", "Jaguars", "Jacksonville"),
    "kc": ("Kansas City Chiefs", "Chiefs", "Kansas City"),
    "lac": ("Los Angeles Chargers", "Chargers", "Los Angeles"),
    "lar": ("Los Angeles Rams", "Rams", "Los Angeles"),
    "lv": ("Las Vegas Raiders", "Raiders", "Las Vegas"),
    "mia": ("Miami Dolphins", "Dolphins", "Miami"),
    "min": ("Minnesota Vikings", "Vikings", "Minnesota"),
    "ne": ("New England Patriots", "Patriots", "New England"),
    "no": ("New Orleans Saints", "Saints", "New Orleans"),
    "nyg": ("New York Giants", "Giants", "New York"),
    "nyj": ("New York Jets", "Jets", "New York"),
    "phi": ("Philadelphia Eagles", "Eagles", "Philadelphia"),
    "pit": ("Pittsburgh Steelers", "Steelers", "Pittsburgh"),
    "sea": ("Seattle Seahawks", "Seahawks", "Seattle"),
    "sf": ("San Francisco 49ers", "49ers", "San Francisco"),
    "tb": ("Tampa Bay Buccaneers", "Buccaneers", "Tampa Bay"),
    "ten": ("Tennessee Titans", "Titans", "Tennessee"),
    "was": ("Washington Commanders", "Commanders", "Washington"),
}

# Cities shared by two franchises cannot identify a team on their own.
_AMBIGUOUS_CITIES = {"los angeles", "new york"}


def _build_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for abbr, (full, nick, city) in TEAMS.items():
        keys = [abbr, full, nick]
        if city.lower() not in _AMBIGUOUS_CITIES:
            keys.append(city)
        for k in keys:
            idx[k.strip().lower()] = abbr
    return idx


_INDEX = _build_index()


def resolve(name: str) -> str | None:
    """Map a team name, nickname, or abbreviation to its Polymarket abbreviation.

    Returns None when the name is unknown or ambiguous (a bare "Los Angeles"),
    which callers must treat as "cannot match" rather than guessing.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _INDEX:
        return _INDEX[key]
    # Fall back to a nickname appearing inside a longer label, e.g.
    # "Detroit Lions D/ST Touchdown". Longest nickname first so that
    # multi-word names win over substrings of themselves.
    for nick in sorted((t[1] for t in TEAMS.values()), key=len, reverse=True):
        if nick.lower() in key:
            return _INDEX[nick.lower()]
    return None


def find_teams(text: str) -> list[str]:
    """Every distinct team referenced in a free-text label, in order of appearance."""
    lowered = (text or "").lower()
    found: list[tuple[int, str]] = []
    for abbr, (full, nick, _city) in TEAMS.items():
        pos = min(
            (lowered.find(s.lower()) for s in (full, nick) if lowered.find(s.lower()) >= 0),
            default=-1,
        )
        if pos >= 0:
            found.append((pos, abbr))
    return [abbr for _pos, abbr in sorted(found)]
