"""Order planning and execution: the cap, the kickoff rule, the ledger."""

from datetime import UTC, datetime, timedelta

import pytest

from nfl_props_arb import ledger
from nfl_props_arb.autotrade import (
    DEFAULT_KICKOFF_BUFFER,
    execute,
    fit_to_budget,
    is_halted,
    plan_orders,
)
from nfl_props_arb.baselines import DEFAULT_AUTOTRADE, Baseline
from nfl_props_arb.combine import max_buy_floor
from nfl_props_arb.edge import Edge
from nfl_props_arb.execution.base import OrderRequest, OrderResult
from nfl_props_arb.execution.dryrun import DryRunOrderClient
from nfl_props_arb.polymarket.client import PmProp
from nfl_props_arb.props import GameKey, PropKey, PropType, Scope
from nfl_props_arb.scan import Opportunity, ScanResult

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
FUTURE = "2026-09-27T20:25:00Z"
PAST = "2026-09-27T11:00:00Z"
GAME = GameKey.from_ticker("nfl-det-buf-2026-09-17")
THETA = 0.0695


def _opportunity(
    prop=PropType.TWO_PT,
    slug="mkt-two-pt",
    kickoff=FUTURE,
    levels=((0.63, 100.0),),
    limit=0.69,
    scope=Scope.GAME,
    team=None,
):
    pm = PmProp(
        key=PropKey(game=GAME, prop=prop, scope=scope, team=team),
        slug=slug,
        market_id="42",
        question="Total Successful Two-Point Conversions: Over 0.5",
        title="Successful Two-Point Conversion?",
        theta=THETA,
        line=0.5,
        event_ticker="nfl-det-buf-2026-09-17",
        start_date=kickoff,
        best_yes_bid=1.0 - levels[0][0],
    )
    floor = max_buy_floor(limit, THETA)
    edges = [
        Edge(no_price=price, floor=floor.value, theta=THETA, qty=qty) for price, qty in levels
    ]
    return Opportunity(
        prop=pm, floor=floor, max_buy=limit, edges=edges, quotes=()
    )


def _result(*opps):
    return ScanResult(
        opportunities=list(opps), considered=len(opps), unpriced=[], generated_at=NOW
    )


# --- the cap ---------------------------------------------------------------


def test_cap_limits_a_single_market():
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10_000.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert sum(o.all_in_cost for o in planned) <= 100.0 + 1e-9


def test_cap_counts_the_fee_not_just_the_price():
    """$100 means $100 leaves the account, fees included."""
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10_000.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert sum(o.notional for o in planned) < 100.0
    assert sum(o.all_in_cost for o in planned) == pytest.approx(100.0, abs=0.02)


def test_existing_exposure_reduces_the_cap():
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10_000.0),))),
        DEFAULT_AUTOTRADE,
        exposure={"mkt-two-pt": 70.0},
        cap=100.0,
        now=NOW,
    )
    assert sum(o.all_in_cost for o in planned) <= 30.0 + 1e-9


def test_a_filled_market_is_not_bought_again():
    """The hourly re-buy this whole file exists to prevent."""
    planned, skipped = plan_orders(
        _result(_opportunity(levels=((0.63, 10_000.0),))),
        DEFAULT_AUTOTRADE,
        exposure={"mkt-two-pt": 100.0},
        cap=100.0,
        now=NOW,
    )
    assert planned == []
    assert skipped[0]["reason"] == "cap_reached"


def test_size_never_exceeds_the_resting_offer():
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 5.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert len(planned) == 1
    assert planned[0].shares == 5.0


def test_deeper_levels_are_taken_after_the_cheapest():
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 5.0), (0.66, 1_000.0)))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert [o.price for o in planned] == [0.63, 0.66]
    assert planned[0].shares == 5.0
    assert sum(o.all_in_cost for o in planned) <= 100.0 + 1e-9


def test_dust_below_minimum_size_is_not_ordered():
    planned, skipped = plan_orders(
        _result(_opportunity(levels=((0.63, 0.001),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned == []
    assert skipped[0]["reason"] == "size_below_minimum"


def test_limit_price_carries_no_float_noise():
    """`1 - yes_bid` produces 0.6699999999999999; that must not reach the wire."""
    noisy = 1.0 - 0.33
    assert noisy != 0.67
    planned, _ = plan_orders(
        _result(_opportunity(levels=((noisy, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned[0].price == 0.67


def test_rounding_never_pushes_a_price_past_the_limit():
    just_under = 0.69 - 1e-12
    planned, _ = plan_orders(
        _result(_opportunity(levels=((just_under, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned[0].price <= 0.69


def test_shares_are_rounded_down_not_nearest():
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 3.567),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned[0].shares == 3.56


# --- what must never be traded ---------------------------------------------


def test_started_games_are_never_planned():
    planned, skipped = plan_orders(
        _result(_opportunity(kickoff=PAST)),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned == []
    assert skipped[0]["reason"] == "started"


def test_imminent_kickoff_is_not_traded_into():
    soon = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    planned, skipped = plan_orders(
        _result(_opportunity(kickoff=soon)),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
        kickoff_buffer=DEFAULT_KICKOFF_BUFFER,
    )
    assert planned == []
    assert skipped[0]["reason"] == "started"


def test_unparseable_kickoff_is_not_traded():
    planned, skipped = plan_orders(
        _result(_opportunity(kickoff="")),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned == []
    assert skipped[0]["reason"] == "started"


def test_overtime_is_never_bought():
    """No operator limit was given for it, so it stays reportable but untraded."""
    planned, skipped = plan_orders(
        _result(_opportunity(prop=PropType.OVERTIME, slug="mkt-ot", limit=0.69)),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert planned == []
    assert skipped[0]["reason"] == "not_tradeable"


def test_a_prop_flagged_untradeable_is_skipped():
    thresholds = dict(DEFAULT_AUTOTRADE)
    thresholds[PropType.TWO_PT] = Baseline(
        scope=Scope.GAME, max_buy=0.69, tradeable=False
    )
    planned, skipped = plan_orders(
        _result(_opportunity()), thresholds, exposure={}, cap=100.0, now=NOW
    )
    assert planned == []
    assert skipped[0]["reason"] == "not_tradeable"


def test_price_above_the_operator_limit_is_refused():
    """Second line of defence behind the scan's own threshold."""
    opp = _opportunity(levels=((0.75, 100.0),))
    planned, skipped = plan_orders(
        _result(opp), DEFAULT_AUTOTRADE, exposure={}, cap=100.0, now=NOW
    )
    assert planned == []
    assert skipped[0]["reason"] == "above_max_buy"


def test_an_offer_exactly_at_the_limit_is_bought():
    """Operator's explicit instruction: 0.69 still qualifies."""
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.69, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    assert len(planned) == 1
    assert planned[0].price == 0.69
    assert planned[0].edge_pts == pytest.approx(0.0, abs=1e-9)


def test_untriggered_opportunities_are_ignored():
    opp = _opportunity()
    opp.edges = []
    planned, _ = plan_orders(
        _result(opp), DEFAULT_AUTOTRADE, exposure={}, cap=100.0, now=NOW
    )
    assert planned == []


# --- execution -------------------------------------------------------------


def test_dry_run_places_nothing_but_records_intent(tmp_path):
    path = tmp_path / "l.jsonl"
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    client = DryRunOrderClient()
    placed = execute(planned, client, run_id="run-1", ledger_path=path, now=NOW)
    assert len(placed) == 1
    assert placed[0][1].status == ledger.DRY_RUN
    assert len(client.submitted) == 1
    # Intent recorded, then resolved as a dry run, so no capital is held.
    assert ledger.load_exposure(path) == {}


def test_orders_are_limit_and_ioc(tmp_path):
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    client = DryRunOrderClient()
    execute(planned, client, run_id="r", ledger_path=tmp_path / "l.jsonl", now=NOW)
    sent = client.submitted[0]
    assert sent.time_in_force == "IOC"
    assert sent.side == "NO"
    assert sent.limit_price == 0.63


def test_kickoff_is_rechecked_before_submission(tmp_path):
    """The plan can be minutes old by the time the last order is sent."""
    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    client = DryRunOrderClient()
    later = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)  # after the 20:25 kickoff
    placed = execute(planned, client, run_id="r", ledger_path=tmp_path / "l.jsonl", now=later)
    assert client.submitted == []
    assert placed[0][1].status == ledger.REJECTED


def test_a_fill_consumes_the_cap_on_the_next_run(tmp_path):
    """End to end: run twice against one ledger and the cap must hold."""
    path = tmp_path / "l.jsonl"

    class Filling:
        name = "stub"

        def place(self, request: OrderRequest) -> OrderResult:
            return OrderResult(
                status="filled",
                order_id="o1",
                filled_shares=request.shares,
                avg_price=request.limit_price,
            )

    for run in ("run-1", "run-2", "run-3"):
        planned, _ = plan_orders(
            _result(_opportunity(levels=((0.63, 10_000.0),))),
            DEFAULT_AUTOTRADE,
            exposure=ledger.load_exposure(path),
            cap=100.0,
            now=NOW,
        )
        execute(planned, Filling(), run_id=run, ledger_path=path, now=NOW)

    total = sum(ledger.load_exposure(path).values())
    assert total <= 100.0 + 1e-9


def test_a_raising_client_leaves_the_intent_on_the_books(tmp_path):
    """An exception is not a rejection: the order may have reached the venue."""
    path = tmp_path / "l.jsonl"

    class Exploding:
        name = "stub"

        def place(self, request: OrderRequest) -> OrderResult:
            raise RuntimeError("connection reset")

    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 10.0),))),
        DEFAULT_AUTOTRADE,
        exposure={},
        cap=100.0,
        now=NOW,
    )
    with pytest.raises(RuntimeError):
        execute(planned, Exploding(), run_id="r", ledger_path=path, now=NOW)
    assert sum(ledger.load_exposure(path).values()) > 0


# --- the kill switch -------------------------------------------------------


def test_halt_file_detected(tmp_path):
    halt = tmp_path / "HALT"
    assert is_halted(halt) is False
    halt.write_text("stop", encoding="utf-8")
    assert is_halted(halt) is True


# --- fills and funds -----------------------------------------------------------


def test_a_partial_fill_commits_only_what_it_took(tmp_path):
    path = tmp_path / "l.jsonl"

    class Partial:
        name = "stub"

        def place(self, request: OrderRequest) -> OrderResult:
            return OrderResult(status="partial", order_id="o1", filled_shares=10.0)

    planned, _ = plan_orders(
        _result(_opportunity(levels=((0.63, 100.0),))),
        DEFAULT_AUTOTRADE, exposure={}, cap=1000.0, now=NOW,
    )
    execute(planned, Partial(), run_id="r", ledger_path=path, now=NOW)
    assert ledger.load_exposure(path)["mkt-two-pt"] == pytest.approx(planned[0].all_in_cost / 10)


def test_budget_keeps_orders_that_fit_and_shrinks_the_one_that_crosses():
    planned, _ = plan_orders(
        _result(
            _opportunity(slug="a", levels=((0.63, 50.0),)),
            _opportunity(slug="b", levels=((0.63, 50.0),)),
            _opportunity(slug="c", levels=((0.63, 50.0),)),
        ),
        DEFAULT_AUTOTRADE, exposure={}, cap=1000.0, now=NOW,
    )
    one = planned[0].all_in_cost
    kept, dropped = fit_to_budget(planned, one * 1.5)
    assert [o.slug for o in kept] == ["a", "b"]
    assert kept[1].shares < planned[1].shares
    assert sum(o.all_in_cost for o in kept) <= one * 1.5 + 1e-9
    assert [d["slug"] for d in dropped] == ["c"]


def test_no_budget_places_nothing():
    planned, _ = plan_orders(
        _result(_opportunity()), DEFAULT_AUTOTRADE, exposure={}, cap=100.0, now=NOW
    )
    kept, dropped = fit_to_budget(planned, 0.0)
    assert kept == [] and len(dropped) == len(planned)


def test_a_fill_logs_its_size_and_no_price(tmp_path):
    path = tmp_path / "l.jsonl"

    class Venue:
        name = "stub"

        def place(self, request: OrderRequest) -> OrderResult:
            # The venue reports avgPx long-side, as observed live.
            return OrderResult(status="partial", order_id="o1", filled_shares=3.0, avg_price=0.37)

    planned, _ = plan_orders(
        _result(_opportunity()), DEFAULT_AUTOTRADE, exposure={}, cap=100.0, now=NOW
    )
    execute(planned, Venue(), run_id="r", ledger_path=path, now=NOW)
    (fill,) = ledger.fills(path)
    assert (fill.shares, fill.fill_price, fill.order_id) == (3.0, 0.63, "o1")
