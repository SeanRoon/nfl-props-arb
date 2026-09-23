"""Fee-adjusted buy limits: the operator supplies a price, not a floor."""

import pytest

from nfl_props_arb.baselines import (
    DEFAULT_AUTOTRADE,
    Baseline,
    apply_overrides,
    load,
    write_autotrade_template,
)
from nfl_props_arb.combine import max_buy_floor
from nfl_props_arb.edge import Edge, effective_cost, max_buy_price
from nfl_props_arb.odds.baseline import BaselineOdds
from nfl_props_arb.props import GameKey, PropType, Scope

GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")
THETAS = (0.06, 0.0695)
LIMITS = (0.69, 0.83)


@pytest.mark.parametrize("limit", LIMITS)
@pytest.mark.parametrize("theta", THETAS)
def test_max_buy_round_trips_through_the_fee_solver(limit, theta):
    """The whole design rests on this: the implied floor inverts back exactly.

    If it did not, an operator's 0.69 would silently become 0.6747 (fee taken
    twice) or 0.7049 (fee never taken).
    """
    floor = max_buy_floor(limit, theta)
    assert max_buy_price(floor.value, theta) == pytest.approx(limit, abs=1e-9)


@pytest.mark.parametrize("limit", LIMITS)
@pytest.mark.parametrize("theta", THETAS)
def test_an_offer_at_exactly_the_limit_is_zero_edge(limit, theta):
    """Operator's call: at the limit it qualifies, and it earns nothing."""
    floor = max_buy_floor(limit, theta)
    edge = Edge(no_price=limit, floor=floor.value, theta=theta, qty=1.0)
    assert edge.edge_pts == pytest.approx(0.0, abs=1e-9)
    assert edge.ev_per_share >= -1e-9


@pytest.mark.parametrize("theta", THETAS)
def test_below_the_limit_is_positive_edge(theta):
    floor = max_buy_floor(0.69, theta)
    assert Edge(no_price=0.63, floor=floor.value, theta=theta, qty=1.0).edge_pts > 0


def test_implied_floor_is_the_all_in_cost():
    floor = max_buy_floor(0.69, 0.0695)
    assert floor.value == pytest.approx(effective_cost(0.69, 0.0695))
    assert floor.method == "max_buy"
    assert floor.max_buy == 0.69


def test_floor_explanation_shows_the_arithmetic():
    assert "operator limit" in max_buy_floor(0.69, 0.0695).explanation


def test_fee_adjusted_floor_is_higher_than_the_raw_limit():
    """Sanity on direction: the floor must exceed the price, never undercut it."""
    floor = max_buy_floor(0.69, 0.0695)
    assert floor.value > 0.69


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_out_of_range_limits_rejected(bad):
    with pytest.raises(ValueError):
        max_buy_floor(bad, 0.06)


def test_baseline_requires_exactly_one_price():
    with pytest.raises(ValueError):
        Baseline(scope=Scope.GAME)
    with pytest.raises(ValueError):
        Baseline(scope=Scope.GAME, american=800, max_buy=0.69)


def test_baseline_rejects_impossible_max_buy():
    with pytest.raises(ValueError):
        Baseline(scope=Scope.GAME, max_buy=1.4)


def test_max_buy_quote_has_no_implied_probability():
    """A price cap is not a probability. Inventing one would fabricate a number."""
    quotes = BaselineOdds(DEFAULT_AUTOTRADE).quotes_for([GAME])
    caps = [q for q in quotes if q.max_buy is not None]
    assert caps
    assert all(q.implied_yes is None and q.american is None for q in caps)


def test_max_buy_is_quoted_at_every_scope():
    """Flat cap: whole-game and each team, which bypasses the leg product."""
    quotes = BaselineOdds(DEFAULT_AUTOTRADE).quotes_for([GAME])
    dst = [q for q in quotes if q.prop is PropType.DST_TD]
    assert {(q.scope, q.team) for q in dst} == {
        (Scope.GAME, None),
        (Scope.TEAM, "det"),
        (Scope.TEAM, "buf"),
    }


def test_american_baselines_still_emit_the_old_way():
    """The scanner's behaviour must not shift underneath it."""
    from nfl_props_arb.baselines import DEFAULT_BASELINES

    quotes = BaselineOdds(DEFAULT_BASELINES).quotes_for([GAME])
    dst = [q for q in quotes if q.prop is PropType.DST_TD]
    assert {(q.scope, q.team) for q in dst} == {(Scope.TEAM, "det"), (Scope.TEAM, "buf")}


def test_overtime_is_not_tradeable_by_default():
    """No operator limit was given for overtime, so it must never be bought."""
    assert DEFAULT_AUTOTRADE[PropType.OVERTIME].tradeable is False
    assert DEFAULT_AUTOTRADE[PropType.OVERTIME].max_buy is None


@pytest.mark.parametrize(
    "prop,limit",
    [
        (PropType.DST_TD, 0.69),
        (PropType.TWO_PT, 0.69),
        (PropType.PICK_SIX, 0.83),
        (PropType.RETURN_TD, 0.83),
        (PropType.SAFETY, 0.83),
    ],
)
def test_operator_limits_are_what_was_asked_for(prop, limit):
    base = DEFAULT_AUTOTRADE[prop]
    assert base.max_buy == limit
    assert base.tradeable is True


def test_decimal_override_sets_max_buy_not_odds():
    table = apply_overrides(DEFAULT_AUTOTRADE, ["two_pt=0.67"])
    assert table[PropType.TWO_PT].max_buy == 0.67
    assert table[PropType.TWO_PT].american is None


def test_integer_override_still_sets_odds():
    table = apply_overrides(DEFAULT_AUTOTRADE, ["two_pt=+350"])
    assert table[PropType.TWO_PT].american == 350
    assert table[PropType.TWO_PT].max_buy is None


def test_autotrade_template_round_trips(tmp_path):
    path = write_autotrade_template(tmp_path / "autotrade.toml")
    loaded = load(path, DEFAULT_AUTOTRADE)
    assert loaded[PropType.DST_TD].max_buy == 0.69
    assert loaded[PropType.PICK_SIX].max_buy == 0.83
    assert loaded[PropType.OVERTIME].tradeable is False


def test_toml_rejects_both_prices(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('[safety]\namerican = 800\nmax_buy = 0.83\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)


def test_toml_rejects_neither_price(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('[safety]\nscope = "game"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)
