"""The leg-product rule and its safety guard."""

import pytest

from nfl_props_arb.combine import (
    IncompleteLegsError,
    Leg,
    direct_floor,
    floor_for,
    game_key_legs,
    leg_product_floor,
)
from nfl_props_arb.props import GameKey, PropKey, PropType, Scope

GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")
KEY = PropKey(GAME, PropType.DST_TD, Scope.GAME)


def _legs(a=0.20, b=0.20):
    return {"det": Leg("det", 400, a), "buf": Leg("buf", 400, b)}


def test_worked_example_from_the_operator():
    """80% NO on each team multiplies to a 64% whole-game floor."""
    floor = leg_product_floor(KEY, _legs())
    assert floor.value == pytest.approx(0.64)
    assert floor.method == "leg_product"
    assert "0.800 x 0.800 = 0.6400" in floor.explanation


def test_product_is_stricter_than_either_leg():
    floor = leg_product_floor(KEY, _legs())
    assert floor.value < min(lg.no_complement for lg in _legs().values())


def test_missing_leg_refuses_to_derive():
    """A partial leg set would overstate the floor and invent an edge."""
    with pytest.raises(IncompleteLegsError) as exc:
        leg_product_floor(KEY, {"det": Leg("det", 400, 0.20)})
    assert exc.value.missing == ("buf",)


def test_no_legs_at_all_refuses():
    with pytest.raises(IncompleteLegsError):
        leg_product_floor(KEY, {})


def test_asymmetric_legs_multiply_correctly():
    floor = leg_product_floor(KEY, _legs(a=0.10, b=0.25))
    assert floor.value == pytest.approx(0.90 * 0.75)


def test_direct_floor_is_the_plain_complement():
    assert direct_floor(0.1333).value == pytest.approx(0.8667, abs=1e-4)


def test_direct_quote_wins_over_the_product():
    """A book pricing the whole-game market beats a product built on independence."""
    floor = floor_for(KEY, direct=0.30, legs=_legs())
    assert floor.method == "direct"
    assert floor.value == pytest.approx(0.70)


def test_falls_back_to_product_without_a_direct_quote():
    assert floor_for(KEY, direct=None, legs=_legs()).method == "leg_product"


def test_team_scope_cannot_use_a_product():
    team_key = PropKey(GAME, PropType.DST_TD, Scope.TEAM, "det")
    with pytest.raises(ValueError):
        leg_product_floor(team_key, _legs())


def test_legs_from_other_games_are_discarded():
    legs = [Leg("det", 400, 0.2), Leg("kc", 400, 0.2)]
    assert set(game_key_legs(GAME, legs)) == {"det"}
