"""The live order client: wire format, signing, and response mapping. Offline.

Transport is an httpx.MockTransport; nothing here reaches the venue.
"""

import base64
import json

import httpx
import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from nfl_props_arb.execution.base import OrderRequest  # noqa: E402
from nfl_props_arb.execution.client import (  # noqa: E402
    LiveOrderClient,
    OrderSpecError,
    long_price_for_no,
)
from nfl_props_arb.execution.credentials import Credentials, load  # noqa: E402

SEED = bytes(range(32))


def _client(handler) -> LiveOrderClient:
    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return LiveOrderClient(Credentials(key_id="kid", private_key=SEED), client=http)


def _request(price=0.83, shares=12.5, side="NO", tif="IOC") -> OrderRequest:
    return OrderRequest(
        slug="mkt-safety", market_id="42", side=side, limit_price=price,
        shares=shares, idempotency_key="k", time_in_force=tif,
    )


def _sync_response(state, cum, avg="0.17"):
    return {
        "id": "ord-1",
        "executions": [
            {"id": "e1", "order": {"id": "ord-1", "state": state, "cumQuantity": cum,
                                   "avgPx": {"value": avg, "currency": "USD"}}},
        ],
    }


# --- price convention --------------------------------------------------------


def test_no_price_is_sent_as_the_long_price():
    """The documented example: buying NO at 0.83 is price.value 0.17."""
    assert long_price_for_no(0.83) == 0.17


def test_float_noise_does_not_move_the_tick():
    assert long_price_for_no(0.6699999999999999) == 0.33


def test_off_tick_no_price_rounds_to_a_stricter_limit():
    """0.835 NO -> 0.17 long (NO 0.83), never 0.16 long (NO 0.84)."""
    assert long_price_for_no(0.835) == 0.17


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 0.001])
def test_unpriceable_no_prices_are_refused(bad):
    with pytest.raises(OrderSpecError):
        long_price_for_no(bad)


# --- request -----------------------------------------------------------------


def test_request_body_matches_the_spec():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        seen["headers"] = req.headers
        return httpx.Response(200, json=_sync_response("ORDER_STATE_FILLED", 12.5))

    _client(handler).place(_request())
    assert seen["path"] == "/v1/orders"
    assert seen["body"] == {
        "marketSlug": "mkt-safety",
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.17", "currency": "USD"},
        "quantity": 12.5,
        "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "intent": "ORDER_INTENT_BUY_SHORT",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "synchronousExecution": True,
        "maxBlockTime": "10",
    }
    assert seen["headers"]["X-PM-Access-Key"] == "kid"


def test_signature_covers_timestamp_method_and_path():
    client = _client(lambda r: httpx.Response(200))
    headers = client.auth_headers("post", "/v1/orders", now_ms=1_700_000_000_000)
    assert headers["X-PM-Timestamp"] == "1700000000000"
    public = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
    public.verify(  # raises if the message differs
        base64.b64decode(headers["X-PM-Signature"]), b"1700000000000POST/v1/orders"
    )


@pytest.mark.parametrize("kwargs", [{"side": "YES"}, {"tif": "GTC"}, {"shares": 0.0}])
def test_anything_but_an_ioc_no_buy_is_refused_before_sending(kwargs):
    def handler(req):  # pragma: no cover - must not be reached
        raise AssertionError("request sent")

    with pytest.raises(OrderSpecError):
        _client(handler).place(_request(**kwargs))


# --- response ------------------------------------------------------------------


def _place(status_code=200, payload=None, text=None):
    def handler(req):
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=payload)

    return _client(handler).place(_request())


def test_full_fill():
    out = _place(payload=_sync_response("ORDER_STATE_FILLED", 12.5))
    assert (out.status, out.order_id, out.filled_shares) == ("filled", "ord-1", 12.5)


def test_partial_fill_then_ioc_cancel():
    out = _place(payload=_sync_response("ORDER_STATE_CANCELED", 4))
    assert (out.status, out.filled_shares) == ("partial", 4.0)


def test_ioc_with_nothing_taken_releases_the_capital():
    out = _place(payload=_sync_response("ORDER_STATE_CANCELED", 0))
    assert out.status == "rejected"


def test_no_executions_is_submitted_not_assumed():
    out = _place(payload={"id": "ord-1"})
    assert (out.status, out.order_id) == ("submitted", "ord-1")


def test_resting_state_is_submitted():
    out = _place(payload=_sync_response("ORDER_STATE_NEW", 0))
    assert out.status == "submitted"


def test_a_4xx_is_a_rejection():
    assert _place(400, text="bad price").status == "rejected"


@pytest.mark.parametrize("code", [409, 500, 503])
def test_duplicate_or_server_error_keeps_capital_committed(code):
    assert _place(code, text="?").status == "submitted"


# --- balances and credentials ---------------------------------------------------


def test_buying_power_reads_the_usd_balance():
    def handler(req):
        assert req.url.path == "/v1/account/balances"
        return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": 116.35}]})

    assert _client(handler).buying_power() == 116.35


def test_64_byte_venue_key_is_reduced_to_its_seed():
    public = Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes_raw()
    raw = base64.b64encode(SEED + public).decode()
    creds = load({"POLYMARKET_US_KEY_ID": "kid", "POLYMARKET_US_KEY": raw})
    assert creds.private_key == SEED


# --- resting bids and cancels ---------------------------------------------------


def test_bid_is_post_only_gtd_short_at_the_long_price():
    from datetime import UTC, datetime, timedelta, timezone

    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "ord-9"})

    eastern = timezone(timedelta(hours=-4))
    out = _client(handler).place_bid(
        "mkt", 0.69, 50.0, datetime(2026, 9, 27, 12, 45, tzinfo=eastern)
    )
    assert (out.status, out.order_id) == ("resting", "ord-9")
    body = seen["body"]
    assert body["price"] == {"value": "0.31", "currency": "USD"}
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert body["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE"
    assert body["goodTillTime"] == "2026-09-27T16:45:00Z"   # converted to UTC
    assert body["participateDontInitiate"] is True
    assert "synchronousExecution" not in body
    assert UTC  # imported for clarity


def test_a_naive_expiry_is_refused():
    from datetime import datetime

    with pytest.raises(OrderSpecError):
        _client(lambda r: httpx.Response(200)).place_bid("m", 0.69, 1.0, datetime(2026, 9, 27))


def test_a_crossing_bid_rejection_is_reported():
    from datetime import UTC, datetime

    out = _client(lambda r: httpx.Response(400, text="would match")).place_bid(
        "m", 0.69, 1.0, datetime(2026, 9, 27, tzinfo=UTC)
    )
    assert out.status == "rejected"


def test_cancel_signs_the_path_with_the_order_id():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    ok, _ = _client(handler).cancel("ORD1", "mkt")
    assert ok and seen["path"] == "/v1/order/ORD1/cancel" and seen["body"] == {"marketSlug": "mkt"}
