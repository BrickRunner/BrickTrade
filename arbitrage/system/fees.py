from __future__ import annotations

import os


def fee_bps(exchange: str, market: str) -> float:
    """Return taker fee bps from env. Example: FEE_BPS_OKX_SPOT=6.0."""
    ex = exchange.upper()
    mk = market.upper()
    key = f"FEE_BPS_{ex}_{mk}"
    if key in os.environ:
        try:
            return float(os.getenv(key, "0") or 0)
        except (TypeError, ValueError):
            return 0.0
    default_key = f"FEE_BPS_{mk}"
    try:
        return float(os.getenv(default_key, "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def fee_bps_from_snapshot(snapshot, exchange: str, market: str, symbol: str | None = None) -> float:
    bucket = snapshot.fee_bps.get(exchange, {})
    if symbol:
        key = f"{market}:{symbol}"
        if key in bucket:
            return float(bucket.get(key) or 0.0)
    return float(bucket.get(market, 0.0) or 0.0)
