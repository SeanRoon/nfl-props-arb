"""The ledger: what stops an hourly job buying the same market sixteen times."""

import pytest

from nfl_props_arb import ledger


def _rec(slug="mkt-a", notional=10.0, status="intent", key="k1", shares=10.0, price=1.0):
    return ledger.OrderRecord(
        ts="2026-09-27T12:00:00+00:00",
        run_id="run-1",
        slug=slug,
        market_id="1",
        game="DET@BUF",
        prop="two_pt",
        side="NO",
        price=price,
        shares=shares,
        notional=notional,
        status=status,
        idempotency_key=key,
    )


def test_absent_ledger_has_no_exposure(tmp_path):
    assert ledger.load_exposure(tmp_path / "nothing.jsonl") == {}


def test_exposure_sums_per_slug(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger.append(_rec(slug="a", notional=10.0, key="k1"), path)
    ledger.append(_rec(slug="a", notional=15.0, key="k2"), path)
    ledger.append(_rec(slug="b", notional=7.0, key="k3"), path)
    assert ledger.load_exposure(path) == {"a": pytest.approx(25.0), "b": pytest.approx(7.0)}


def test_rejection_releases_the_capital(tmp_path):
    """An order the venue refused is not a position and must not block the cap."""
    path = tmp_path / "l.jsonl"
    ledger.append(_rec(notional=40.0, status="intent", key="k1"), path)
    ledger.append(_rec(notional=40.0, status=ledger.REJECTED, key="k1"), path)
    assert ledger.load_exposure(path) == {}


def test_latest_status_for_a_key_wins(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger.append(_rec(notional=40.0, status="intent", key="k1"), path)
    ledger.append(_rec(notional=40.0, status="filled", key="k1"), path)
    assert ledger.load_exposure(path) == {"mkt-a": pytest.approx(40.0)}


def test_dry_runs_do_not_consume_the_cap(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger.append(_rec(notional=40.0, status=ledger.DRY_RUN, key="k1"), path)
    assert ledger.load_exposure(path) == {}


def test_a_corrupt_tail_does_not_hide_earlier_exposure(tmp_path):
    """A half-written line from a killed process must not disable the cap."""
    path = tmp_path / "l.jsonl"
    ledger.append(_rec(notional=30.0, key="k1"), path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-27T12:00:0')
    assert ledger.load_exposure(path) == {"mkt-a": pytest.approx(30.0)}


def test_idempotency_key_is_stable_within_a_run():
    a = ledger.idempotency_key("run-1", "slug", 0.69, 10.0)
    b = ledger.idempotency_key("run-1", "slug", 0.69, 10.0)
    assert a == b


def test_idempotency_key_differs_across_runs():
    """Next hour's order at the same price genuinely is a different order."""
    a = ledger.idempotency_key("run-1", "slug", 0.69, 10.0)
    b = ledger.idempotency_key("run-2", "slug", 0.69, 10.0)
    assert a != b


@pytest.mark.parametrize(
    "field,value",
    [("slug", "other"), ("price", 0.70), ("shares", 11.0)],
)
def test_idempotency_key_covers_every_order_term(field, value):
    args = {"run_id": "r", "slug": "s", "price": 0.69, "shares": 10.0}
    base = ledger.idempotency_key(**args)
    assert ledger.idempotency_key(**{**args, field: value}) != base


def test_records_round_trip(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger.append(_rec(key="k1"), path)
    got = list(ledger.read(path))
    assert len(got) == 1 and got[0].idempotency_key == "k1"
