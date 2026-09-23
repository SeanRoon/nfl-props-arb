"""The order-placement seam.

Everything above this module -- selection, sizing, the cap, the ledger -- works
without any venue credentials and is fully exercisable with `--dry-run`. Only the
final call needs the authenticated API.

Orders are **limit, immediate-or-cancel, priced at the level being taken**. Never
market orders. The entire strategy is a price threshold; an order that can fill
above its limit defeats the thing it exists to enforce. Immediate-or-cancel
because a resting order would still be live after kickoff, and this tool's whole
premise is that it only trades games that have not started.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class OrderRequest:
    """One order to place."""

    slug: str
    market_id: str
    side: str            # "NO"
    limit_price: float   # never fill above this
    shares: float
    idempotency_key: str
    time_in_force: str = "IOC"

    @property
    def notional(self) -> float:
        return self.limit_price * self.shares


@dataclass(frozen=True)
class OrderResult:
    """What the venue said. `status` is the ledger status to record."""

    status: str                 # "filled" | "partial" | "rejected" | "submitted" | "dry_run"
    order_id: str | None = None
    filled_shares: float = 0.0
    avg_price: float | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class OrderClient(Protocol):
    """Anything that can place an order, real or simulated."""

    name: str

    def place(self, request: OrderRequest) -> OrderResult:
        """Submit one order and report what happened."""
        ...
