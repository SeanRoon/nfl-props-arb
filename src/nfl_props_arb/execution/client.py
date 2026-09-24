"""Authenticated client for Polymarket US order placement.

Written to the venue's published spec (docs.polymarket.us, read 2026-09-23):

  * `POST /v1/orders`, JSON body.
  * Auth is three headers: `X-PM-Access-Key` (key id), `X-PM-Timestamp` (Unix
    **milliseconds**, within 30 s of server time) and `X-PM-Signature`, a base64
    Ed25519 signature over `timestamp + METHOD + path`. The body is not signed.
  * `price.value` is **always the long (YES) price, whatever the intent**. To buy
    NO at X the order says `ORDER_INTENT_BUY_SHORT` at `1 - X`. Sending the NO
    price as-is does not fail -- it buys at the wrong price. `long_price_for_no`
    is the single place this is encoded.
  * Executions come back in the response only when `synchronousExecution` is
    set; otherwise the reply is an order id and nothing about fills.

Observed on the first live fill (2026-09-23, order CNYHQK5AAWP6):

  * `avgPx` on a short order **is quoted long-side** too: NO filled at 0.69
    reported `avgPx` 0.31. It is passed through raw in the result message and
    never used for the cap, which is computed from our own limit price and the
    filled quantity.

Not documented, so treated conservatively rather than guessed:

  * The error body shape. Any 4xx other than 409 is treated as a refusal; 409
    (duplicate) and 5xx are ambiguous and leave the capital committed.
"""

from __future__ import annotations

import base64
import json
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .base import OrderRequest, OrderResult
from .credentials import Credentials

API_BASE = "https://api.polymarket.us"
ORDERS_PATH = "/v1/orders"
BALANCES_PATH = "/v1/account/balances"
OPEN_ORDERS_PATH = "/v1/orders/open"

# Every NFL prop market observed carries orderPriceMinTickSize = 0.01.
PRICE_TICK = 0.01

TIME_IN_FORCE = {"IOC": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"}
INTENT = {"NO": "ORDER_INTENT_BUY_SHORT"}

# Seconds the venue may hold the request open while an IOC order resolves.
MAX_BLOCK_SECONDS = 10

_DONE_EMPTY = {"ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED", "ORDER_STATE_REJECTED"}


class OrderSpecError(ValueError):
    """Raised for an order this client refuses to translate."""


def long_price_for_no(no_price: float, tick: float = PRICE_TICK) -> float:
    """The `price.value` that buys NO at no more than `no_price`.

    A NO buy at X is a YES sale at 1 - X, so the long price is rounded **up** to
    the tick: a higher long price is a lower NO price, and the limit can only get
    stricter. Rounding the other way would let the order fill above max_buy.
    """
    if not 0.0 < no_price < 1.0:
        raise OrderSpecError(f"NO price out of range: {no_price}")
    ticks = math.ceil((1.0 - no_price) / tick - 1e-9)
    long_price = round(ticks * tick, 6)
    if not 0.0 < long_price < 1.0:
        raise OrderSpecError(f"NO price {no_price} has no valid long price on a {tick} tick")
    return long_price


class LiveOrderClient:
    """Signs and sends real orders. Requires KYC and an Ed25519 key."""

    name = "polymarket-us"

    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str = API_BASE,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "nfl-props-arb/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LiveOrderClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sign(self, payload: bytes) -> str:
        """Ed25519 signature over `payload`, base64-encoded."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "live orders need the 'execute' extra: uv sync --extra execute"
            ) from exc

        key = Ed25519PrivateKey.from_private_bytes(self._credentials.private_key)
        return base64.b64encode(key.sign(payload)).decode("ascii")

    def auth_headers(self, method: str, path: str, *, now_ms: int | None = None) -> dict[str, str]:
        """The three auth headers for one request. `path` excludes the host."""
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        return {
            "X-PM-Access-Key": self._credentials.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": self.sign(f"{ts}{method.upper()}{path}".encode()),
        }

    def buying_power(self) -> float:
        """USD buying power. Read-only; raises on any failure."""
        response = self._client.get(BALANCES_PATH, headers=self.auth_headers("GET", BALANCES_PATH))
        response.raise_for_status()
        for bal in response.json().get("balances") or []:
            if bal.get("currency") == "USD":
                return float(bal["buyingPower"])
        raise RuntimeError("no USD balance in /v1/account/balances response")

    def place_bid(
        self, slug: str, no_price: float, shares: float, expires_at: datetime
    ) -> OrderResult:
        """Rest a post-only NO bid that the venue expires at `expires_at`.

        Not IOC and not synchronous: the order is meant to sit on the book. A 2xx
        with an order id is `resting`; anything else is reported, never retried.
        """
        if shares <= 0:
            raise OrderSpecError(f"non-positive size {shares}")
        if expires_at.tzinfo is None:
            raise OrderSpecError("expiry must be timezone-aware")
        body = {
            "marketSlug": slug,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{long_price_for_no(no_price):.2f}", "currency": "USD"},
            "quantity": shares,
            "tif": "TIME_IN_FORCE_GOOD_TILL_DATE",
            # Format as seen on live GTD orders: ISO 8601, UTC, whole seconds.
            "goodTillTime": expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "intent": "ORDER_INTENT_BUY_SHORT",
            "participateDontInitiate": True,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.auth_headers("POST", ORDERS_PATH)}
        response = self._client.post(ORDERS_PATH, content=raw, headers=headers)
        code = response.status_code
        if code == 409 or code >= 500:
            return OrderResult(
                status="submitted",
                message=f"HTTP {code}, outcome unknown, check open orders: {response.text[:400]}",
            )
        if code >= 400:
            return OrderResult(status="rejected", message=f"HTTP {code}: {response.text[:400]}")
        payload = json.loads(response.content.decode("utf-8"))
        order_id = payload.get("id")
        if not order_id:
            return OrderResult(status="submitted", message="no order id returned", raw=payload)
        return OrderResult(status="resting", order_id=order_id, raw=payload)

    def open_orders(self) -> list[dict[str, Any]]:
        """Every open order on the account. Read-only; raises on any failure."""
        headers = self.auth_headers("GET", OPEN_ORDERS_PATH)
        response = self._client.get(OPEN_ORDERS_PATH, headers=headers)
        response.raise_for_status()
        orders: list[dict[str, Any]] = response.json().get("orders") or []
        return orders

    def cancel(self, order_id: str, slug: str) -> tuple[bool, str]:
        """Cancel one order. Returns (ok, detail)."""
        path = f"/v1/order/{order_id}/cancel"
        raw = json.dumps({"marketSlug": slug}, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.auth_headers("POST", path)}
        response = self._client.post(path, content=raw, headers=headers)
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}: {response.text[:300]}"
        return True, "cancelled"

    def _build_request(self, request: OrderRequest) -> tuple[str, dict[str, Any]]:
        """Map an OrderRequest onto the venue's wire format."""
        intent = INTENT.get(request.side)
        if intent is None:
            raise OrderSpecError(f"unsupported side {request.side!r}; this tool buys NO only")
        tif = TIME_IN_FORCE.get(request.time_in_force)
        if tif is None:
            raise OrderSpecError(f"unsupported time in force {request.time_in_force!r}")
        if request.shares <= 0:
            raise OrderSpecError(f"non-positive size {request.shares}")
        return ORDERS_PATH, {
            "marketSlug": request.slug,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{long_price_for_no(request.limit_price):.2f}", "currency": "USD"},
            "quantity": request.shares,
            "tif": tif,
            "intent": intent,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": str(MAX_BLOCK_SECONDS),
        }

    def _parse_response(self, payload: dict[str, Any]) -> OrderResult:
        """Map the venue's response onto an OrderResult.

        The last execution carries the order's latest state. With nothing to go on
        the order is `submitted`: accepted, outcome unknown, capital still counted.
        """
        order_id = payload.get("id")
        executions = payload.get("executions") or []
        if not executions:
            return OrderResult(
                status="submitted",
                order_id=order_id,
                message="accepted; no executions returned, reconcile manually",
                raw=payload,
            )
        order = executions[-1].get("order") or {}
        state = order.get("state") or ""
        filled = float(order.get("cumQuantity") or 0.0)
        avg_px = (order.get("avgPx") or {}).get("value")
        detail = f"{state} filled={filled:g} venue_avgPx={avg_px}"

        if state == "ORDER_STATE_FILLED":
            status = "filled"
        elif filled > 0:
            status = "partial"
        elif state in _DONE_EMPTY:
            # IOC with nothing taken: nothing happened, so the capital is released.
            status = "rejected"
        else:
            status = "submitted"
        return OrderResult(
            status=status,
            order_id=order_id or order.get("id"),
            filled_shares=filled,
            avg_price=float(avg_px) if avg_px is not None else None,
            message=detail,
            raw=payload,
        )

    def place(self, request: OrderRequest) -> OrderResult:
        """Submit one order.

        Deliberately does not retry. A timeout is not a rejection: the order may
        well have been accepted, and a blind retry is how one intended position
        becomes two. The caller records the intent before calling this, so an
        ambiguous failure leaves the capital committed in the ledger and the
        operator to reconcile it. That is the safe direction to be wrong in.
        """
        path, body = self._build_request(request)
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.auth_headers("POST", path)}
        response = self._client.post(path, content=raw, headers=headers)
        code = response.status_code
        if code == 409 or code >= 500:
            # Duplicate, or the server failed mid-request: the order may exist.
            return OrderResult(
                status="submitted",
                message=f"HTTP {code}, outcome unknown, reconcile: {response.text[:400]}",
            )
        if code >= 400:
            return OrderResult(status="rejected", message=f"HTTP {code}: {response.text[:400]}")
        return self._parse_response(json.loads(response.content.decode("utf-8")))
