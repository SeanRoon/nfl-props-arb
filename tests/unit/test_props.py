"""Game keys, prop keys, and the Polymarket market-type map."""

import pytest

from nfl_props_arb.props import (
    PM_MARKET_TYPES,
    REQUIRED_LINE,
    GameKey,
    PropKey,
    PropType,
    Scope,
)


def test_parses_a_game_ticker():
    g = GameKey.from_ticker("nfl-det-buf-2026-09-17")
    assert (g.away, g.home, g.date) == ("det", "buf", "2026-09-17")
    assert g.teams == ("det", "buf")


def test_away_team_comes_first():
    """Polymarket lists NFL away-first (ordering: "away" in /v1/sports)."""
    assert GameKey.from_ticker("nfl-det-buf-2026-09-17").away == "det"


def test_rejects_a_futures_ticker():
    assert GameKey.from_ticker("nfl-afcwest-2027-01-10-w") is None


def test_rejects_another_league():
    assert GameKey.from_ticker("nba-bos-lal-2026-09-17") is None


def test_team_scope_requires_a_team():
    game = GameKey.from_ticker("nfl-det-buf-2026-09-17")
    with pytest.raises(ValueError):
        PropKey(game, PropType.DST_TD, Scope.TEAM)


def test_game_scope_rejects_a_team():
    game = GameKey.from_ticker("nfl-det-buf-2026-09-17")
    with pytest.raises(ValueError):
        PropKey(game, PropType.DST_TD, Scope.GAME, "det")


def test_team_key_rolls_up_to_its_game_key():
    game = GameKey.from_ticker("nfl-det-buf-2026-09-17")
    key = PropKey(game, PropType.DST_TD, Scope.TEAM, "det")
    rolled = key.as_game
    assert rolled.scope is Scope.GAME and rolled.team is None


def test_all_six_props_are_mapped():
    assert {p for p, _ in PM_MARKET_TYPES.values()} == set(PropType)


def test_dst_td_is_the_only_prop_with_both_scopes():
    both = {p for p, s in PM_MARKET_TYPES.items() if s[1] is Scope.TEAM}
    assert both == {"football_team_total_defensive_special_teams_touchdowns"}


def test_required_line_is_at_least_one():
    """Over 0.5 means "at least one". An Over 1.5 market is a different bet."""
    assert REQUIRED_LINE == 0.5
