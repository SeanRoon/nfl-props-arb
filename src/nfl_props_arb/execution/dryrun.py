"""An order client that places nothing.

This is the default, and it is not a stub: every step before submission runs for
real against the live slate -- discovery, pricing, the kickoff filter, cap
arithmetic against the real ledger. The only thing that does not happen is the
money moving. That makes a dry run worth reading rather than a formality.
"""

from __future__ import annotations

from .base import OrderRequest, OrderResult


class DryRunOrderClient:
    """Records what would have been sent."""

    name = "dry-run"

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []

    def place(self, request: OrderRequest) -> OrderResult:
        self.submitted.append(request)
        return OrderResult(
            status="dry_run",
            order_id=None,
            filled_shares=0.0,
            message=(
                f"DRY RUN: would buy {request.shares:.2f} {request.side} "
                f"@ {request.limit_price:.4f} ({request.time_in_force}) "
                f"= ${request.notional:.2f} on {request.slug}"
            ),
        )
