"""Persist each scan to Parquet.

No public price history exists for these props, so the backtest substrate has to
be accumulated. Recording is cheap now and is the only way to answer, in a
month, whether the longshot bias is real and persistent rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .scan import ScanResult


def to_frame(result: ScanResult) -> pd.DataFrame:
    """One row per priced market, whether or not it triggered."""
    rows = []
    for o in result.opportunities:
        best = o.best
        rows.append(
            {
                "generated_at": result.generated_at,
                "game": str(o.prop.key.game),
                "kickoff": o.prop.start_date,
                "prop": o.prop.key.prop.value,
                "scope": o.prop.key.scope.value,
                "team": o.prop.key.team,
                "slug": o.prop.slug,
                "question": o.prop.question,
                "theta": o.prop.theta,
                "no_ask": o.prop.no_ask,
                "floor": o.floor.value,
                "floor_method": o.floor.method,
                "floor_explanation": o.floor.explanation,
                "fanduel": o.fd_summary,
                "max_buy": o.max_buy,
                "triggered": o.triggered,
                "best_edge_pts": best.edge_pts if best else None,
                "best_roi": best.roi if best else None,
                "qualifying_shares": o.total_shares,
                "qualifying_cost": o.total_cost,
                "qualifying_ev": o.total_ev,
            }
        )
    return pd.DataFrame(rows)


def write_snapshot(result: ScanResult, out_dir: Path | str = Path("data/scans")) -> Path:
    """Write to ``<out_dir>/YYYY-MM-DD/HHMM.parquet``."""
    ts = result.generated_at
    day = Path(out_dir) / ts.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{ts.strftime('%H%M')}.parquet"
    to_frame(result).to_parquet(path, index=False, compression="zstd")
    return path
