from nfl_props_arb.teams import TEAMS, find_teams, resolve


def test_has_all_thirty_two_teams():
    assert len(TEAMS) == 32


def test_resolves_full_name_nickname_and_abbreviation():
    assert resolve("Detroit Lions") == "det"
    assert resolve("Lions") == "det"
    assert resolve("det") == "det"


def test_resolves_a_nickname_inside_a_longer_label():
    assert resolve("Detroit Lions D/ST Touchdown") == "det"


def test_shared_cities_refuse_to_guess():
    """Two franchises share these cities, so the city alone cannot identify one."""
    assert resolve("Los Angeles") is None
    assert resolve("New York") is None


def test_unshared_city_resolves():
    assert resolve("Green Bay") == "gb"


def test_unknown_name_returns_none():
    assert resolve("Toronto Argonauts") is None
    assert resolve("") is None


def test_numeric_nickname_resolves():
    assert resolve("49ers") == "sf"


def test_find_teams_returns_both_sides_in_order():
    assert find_teams("Detroit Lions @ Buffalo Bills") == ["det", "buf"]
