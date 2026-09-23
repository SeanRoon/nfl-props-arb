"""Order placement.

This is the only part of the package that can move money. Everything else --
discovery, pricing, reporting, snapshots -- remains read-only, and
`polymarket/client.py` in particular still touches nothing but the public
gateway.
"""

from .base import OrderClient, OrderRequest, OrderResult
from .dryrun import DryRunOrderClient

__all__ = ["DryRunOrderClient", "OrderClient", "OrderRequest", "OrderResult"]
