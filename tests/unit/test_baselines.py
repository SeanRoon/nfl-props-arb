"""Baseline odds: the stand-in for a live book feed."""

import pytest

from nfl_props_arb.baselines import (
    DEFAULT_BASELINES,
    Baseline,
    apply_overrides,
    load,
    write_template,
)
from nfl_props_arb.edge import american_to_prob, required_american_odds
from nfl_props_arb.odds.baseline import BaselineOdds
from nfl_props_arb.props import GameKey, PropType, Scope

GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")


def test_every_prop_has_a_baseline():
    assert set(DEFAULT_BASELINES) == set(PropType)


def test_dst_td_is_priced_per_team():
    """FanDuel prices D/ST TD per team, so the baseline must too."""
    assert DEFAULT_BASELINES[PropType.DST_TD].scope is Scope.TEAM


def test_team_scoped_props_emit_one_quote_per_team():
    quotes = BaselineOdds(DEFAULT_BASELINES).quotes_for([GAME])
    dst = [q for q in quotes if q.prop is PropType.DST_TD]
    assert {q.team for q in dst} == {"det", "buf"}


def test_game_scoped_props_emit_one_quote():
    quotes = BaselineOdds(DEFAULT_BASELINES).quotes_for([GAME])
    safety = [q for q in quotes if q.prop is PropType.SAFETY]
    assert len(safety) == 1 and safety[0].team is None


def test_quotes_are_labelled_as_baseline():
    """Downstream must be able to tell an assumption from a live quote."""
    quotes = BaselineOdds(DEFAULT_BASELINES).quotes_for([GAME])
    assert all(q.source == "baseline" for q in quotes)


def test_shorter_baseline_is_the_conservative_direction():
    """A shorter assumed price lowers the floor, making the scan stricter."""
    short = 1 - american_to_prob(400)
    long_ = 1 - american_to_prob(1200)
    assert short < long_


def test_overrides_replace_odds_but_keep_scope():
    table = apply_overrides(DEFAULT_BASELINES, ["dst_td=+850"])
    assert table[PropType.DST_TD].american == 850
    assert table[PropType.DST_TD].scope is Scope.TEAM


def test_override_accepts_no_plus_sign():
    assert apply_overrides(DEFAULT_BASELINES, ["two_pt=300"])[PropType.TWO_PT].american == 300


def test_malformed_override_is_rejected():
    with pytest.raises(ValueError):
        apply_overrides(DEFAULT_BASELINES, ["dst_td"])


def test_round_trips_through_toml(tmp_path):
    path = write_template(tmp_path / "baselines.toml")
    loaded = load(path)
    assert set(loaded) == set(PropType)
    assert loaded[PropType.DST_TD].scope is Scope.TEAM


def test_missing_file_falls_back_to_defaults(tmp_path):
    assert load(tmp_path / "absent.toml") == DEFAULT_BASELINES


def test_zero_odds_rejected(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('[safety]\namerican = 0\nscope = "game"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)


def test_unknown_prop_rejected(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('[touchdown_dance]\namerican = 500\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)


@pytest.mark.parametrize("no_price,expected", [(0.63, 181), (0.83, 519), (0.86, 653)])
def test_required_odds_is_the_number_to_check_against_a_book(no_price, expected):
    """The manual-verification shortcut: one line to compare against the app."""
    assert required_american_odds(no_price, theta=0.06) == expected


def test_required_odds_round_trips_to_break_even():
    """At exactly the required odds, edge is zero."""
    from nfl_props_arb.edge import Edge

    odds = required_american_odds(0.80, theta=0.06)
    floor = 1 - american_to_prob(odds)
    assert Edge(no_price=0.80, floor=floor, theta=0.06, qty=1).edge_pts == pytest.approx(0, abs=0.05)


def test_baseline_dataclass_defaults_min_edge_to_zero():
    assert Baseline(american=500, scope=Scope.GAME).min_edge_pts == 0.0
