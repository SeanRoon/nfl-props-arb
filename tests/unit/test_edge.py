"""Core math. These numbers are the contract the whole tool rests on."""

import pytest

from nfl_props_arb.edge import (
    Edge,
    american_to_prob,
    effective_cost,
    fee_per_share,
    max_buy_price,
    no_levels_from_yes_bids,
    prob_to_american,
    qualifying_levels,
)


def test_american_to_prob_underdog():
    assert american_to_prob(650) == pytest.approx(0.133333, abs=1e-6)


def test_american_to_prob_favourite():
    assert american_to_prob(-200) == pytest.approx(0.666667, abs=1e-6)


def test_american_to_prob_even_money():
    assert american_to_prob(100) == pytest.approx(0.5)
    assert american_to_prob(-100) == pytest.approx(0.5)


def test_american_zero_rejected():
    with pytest.raises(ValueError):
        american_to_prob(0)


def test_american_round_trip():
    for odds in (-500, -200, -110, 100, 250, 650, 2000):
        assert prob_to_american(american_to_prob(odds)) == odds


def test_fee_is_symmetric_about_half():
    assert fee_per_share(0.30) == pytest.approx(fee_per_share(0.70))


def test_fee_at_the_working_price():
    # theta 0.06 at a NO price of 0.862 is 0.71 points, not the 0.5 a flat
    # buffer would assume.
    assert fee_per_share(0.862, 0.06) == pytest.approx(0.007138, abs=1e-6)


def test_break_even_beats_a_flat_half_percent_buffer():
    """The headline finding: 0.862 is -EV against a +650 floor once fees apply."""
    floor = 1 - american_to_prob(650)
    assert floor == pytest.approx(0.866667, abs=1e-6)
    assert max_buy_price(floor, theta=0.06) == pytest.approx(0.859418, abs=1e-6)
    assert effective_cost(0.862, 0.06) > floor  # the naive threshold loses money


def test_max_buy_price_with_zero_fee_is_the_floor():
    assert max_buy_price(0.8667, theta=0.0) == pytest.approx(0.8667)


def test_max_buy_price_honours_slippage():
    floor = 0.866667
    assert max_buy_price(floor, 0.06, slippage=0.01) < max_buy_price(floor, 0.06)


def test_no_ladder_mirrors_yes_bids():
    """A resting YES bid at 0.35 is a resting NO offer at 0.65."""
    levels = no_levels_from_yes_bids([(0.35, 0.02), (0.33, 5.0), (0.01, 900.0)])
    assert [round(lv.price, 2) for lv in levels] == [0.65, 0.67, 0.99]
    assert levels[0].qty == 0.02  # cheapest NO first
    assert levels[0].yes_bid == 0.35


def test_no_ladder_drops_zero_quantity_levels():
    assert no_levels_from_yes_bids([(0.35, 0.0)]) == []


def test_qualifying_levels_keeps_a_single_share():
    """Liquidity is reported, never used to suppress a signal."""
    levels = no_levels_from_yes_bids([(0.20, 1.0), (0.05, 5000.0)])
    qualifying = qualifying_levels(levels, max_price=0.85)
    assert len(qualifying) == 1
    assert qualifying[0].qty == 1.0


def test_edge_accounts_for_fees():
    e = Edge(no_price=0.80, floor=0.866667, theta=0.06, qty=10)
    assert e.all_in == pytest.approx(0.8096, abs=1e-4)
    assert e.edge_pts == pytest.approx(5.707, abs=1e-3)
    assert e.is_positive


def test_edge_goes_negative_at_the_naive_threshold():
    e = Edge(no_price=0.862, floor=0.866667, theta=0.06, qty=1)
    assert not e.is_positive
    assert e.edge_pts < 0
