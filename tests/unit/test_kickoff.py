"""The live-game filter.

Unattended, this is the guard that matters most: a NO offer on a game that
already kicked off can look cheap precisely because the event has happened.
"""

from datetime import UTC, datetime, timedelta

import pytest

from nfl_props_arb.polymarket.client import PmProp
from nfl_props_arb.props import GameKey, PropKey, PropType, Scope

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)
GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")


def _prop(start_date: str) -> PmProp:
    return PmProp(
        key=PropKey(game=GAME, prop=PropType.SAFETY, scope=Scope.GAME, team=None),
        slug="astatc-nfl-det-buf-2026-09-17-safety-0pt5",
        market_id="1",
        question="Safety?: Over 0.5",
        title="Safety",
        theta=0.0695,
        line=0.5,
        event_ticker="nfl-det-buf-2026-09-17",
        start_date=start_date,
    )


def test_future_kickoff_has_not_started():
    assert _prop("2026-09-27T20:25:00Z").has_started(NOW) is False


def test_past_kickoff_has_started():
    assert _prop("2026-09-27T17:00:00Z").has_started(NOW) is True


def test_kickoff_exactly_now_counts_as_started():
    assert _prop("2026-09-27T18:00:00Z").has_started(NOW) is True


def test_buffer_stops_trading_into_an_imminent_kickoff():
    prop = _prop("2026-09-27T18:10:00Z")
    assert prop.has_started(NOW) is False
    assert prop.has_started(NOW, timedelta(minutes=15)) is True


@pytest.mark.parametrize("raw", ["", "   ", "not-a-date", "2026-13-45T99:99:99Z", "soon"])
def test_unparseable_kickoff_counts_as_started(raw):
    """Fail closed. A kickoff that cannot be established cannot be ruled out."""
    assert _prop(raw).has_started(NOW) is True
    assert _prop(raw).kickoff_at is None


def test_naive_timestamps_are_read_as_utc():
    assert _prop("2026-09-27T20:25:00").kickoff_at == datetime(
        2026, 9, 27, 20, 25, tzinfo=UTC
    )


def test_offset_timestamps_are_respected():
    prop = _prop("2026-09-27T16:25:00-04:00")
    assert prop.kickoff_at == datetime(2026, 9, 27, 20, 25, tzinfo=UTC)
    assert prop.has_started(NOW) is False


def test_events_filter_drops_started_games(nfl_event):
    """Against the recorded fixture, with the clock set past its kickoff."""
    from nfl_props_arb.polymarket.client import PolymarketUS

    events = nfl_event["events"]
    client = PolymarketUS()
    try:
        kept, _ = client.props_from_events(events, exclude_started=False)
        assert kept, "fixture should yield priceable props"
        kickoff = kept[0].kickoff_at
        assert kickoff is not None

        after = kickoff + timedelta(hours=1)
        dropped, skipped = client.props_from_events(
            events, exclude_started=True, now=after
        )
        assert dropped == []
        assert "started" in {s["reason"] for s in skipped}

        before = kickoff - timedelta(hours=1)
        still_open, _ = client.props_from_events(
            events, exclude_started=True, now=before
        )
        assert len(still_open) == len(kept)
    finally:
        client.close()
