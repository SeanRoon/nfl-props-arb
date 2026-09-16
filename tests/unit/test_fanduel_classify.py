"""FanDuel market-name classification, where precedence is load-bearing."""

import pytest

from nfl_props_arb.odds.fanduel import classify_market_name as classify
from nfl_props_arb.props import PropType


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Any Defensive or Special Teams Touchdown", PropType.DST_TD),
        ("Defensive/Special Teams TD", PropType.DST_TD),
        ("Pick Six", PropType.PICK_SIX),
        ("Will There Be a Pick-6?", PropType.PICK_SIX),
        ("Kickoff or Punt Return Touchdown", PropType.RETURN_TD),
        ("Punt Return Touchdown", PropType.RETURN_TD),
        ("Will There Be a Safety?", PropType.SAFETY),
        ("Will the Game Go to Overtime?", PropType.OVERTIME),
        ("Successful 2-Point Conversion", PropType.TWO_PT),
        ("Any Two-Point Conversion", PropType.TWO_PT),
        # Books abbreviate freely; both spellings must classify.
        ("Any D/ST TD", PropType.DST_TD),
        ("Kickoff Return TD", PropType.RETURN_TD),
    ],
)
def test_classifies_target_markets(name, expected):
    assert classify(name) == expected


def test_pick_six_is_not_absorbed_by_the_general_dst_rule():
    """A pick six is a defensive touchdown, but it is a narrower market."""
    assert classify("Pick Six Defensive Touchdown") is PropType.PICK_SIX


def test_return_touchdown_is_not_absorbed_either():
    assert classify("Special Teams Kickoff Return Touchdown") is PropType.RETURN_TD


@pytest.mark.parametrize(
    "name",
    [
        "Anytime Touchdown Scorer",
        "Total Points Over/Under",
        "Alternate Spread",
        "First Team to Score",
        "",
    ],
)
def test_ignores_markets_we_do_not_price(name):
    assert classify(name) is None


def test_td_inside_a_word_does_not_count():
    """The TD abbreviation is word-bounded, so it cannot match mid-word."""
    assert classify("Standard Defensive Coverage") is None
