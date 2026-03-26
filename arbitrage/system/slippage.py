from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class SlippageModel:
    base_bps: float = 1.5
    depth_bps_factor: float = 4.0
    volatility_bps_factor: float = 3.0
    latency_bps_factor: float = 0.5

    def estimate(
        self,
        order_notional_usd: float,
        average_book_depth_usd: float,
        volatility: float,
        latency_ms: float,
    ) -> float:
        if average_book_depth_usd <= 0:
            return 1000.0
        size_pressure = min(1.0, order_notional_usd / average_book_depth_usd)
        latency_scaled = max(0.0, latency_ms / 1000.0)
        return (
            self.base_bps
            + size_pressure * self.depth_bps_factor
            + max(0.0, volatility) * self.volatility_bps_factor
            + latency_scaled * self.latency_bps_factor
        )

    @staticmethod
    def walk_book(
        levels: List[Tuple[float, float]],
        notional_usd: float,
    ) -> float:
        """Walk orderbook levels to compute volume-weighted average fill price.

        Args:
            levels: list of (price, quantity) tuples — bids or asks from the book.
                    For a BUY, pass asks (ascending).  For a SELL, pass bids (descending).
            notional_usd: total USD value to fill.

        Returns:
            Volume-weighted average fill price, or 0.0 if the book is empty
            or has insufficient depth.
        """
        if not levels or notional_usd <= 0:
            return 0.0

        filled_usd = 0.0
        filled_qty = 0.0

        for price, qty in levels:
            price = float(price)
            qty = float(qty)
            if price <= 0 or qty <= 0:
                continue

            level_usd = price * qty
            remaining = notional_usd - filled_usd

            if remaining <= 0:
                break

            if level_usd >= remaining:
                # Partially consume this level
                partial_qty = remaining / price
                filled_qty += partial_qty
                filled_usd += remaining
            else:
                # Fully consume this level
                filled_qty += qty
                filled_usd += level_usd

        if filled_qty <= 0:
            return 0.0

        return filled_usd / filled_qty

    @staticmethod
    def walk_book_slippage_bps(
        levels: List[Tuple[float, float]],
        notional_usd: float,
        top_of_book_price: float,
    ) -> float:
        """Compute slippage in basis points from walking the book vs top-of-book.

        Args:
            levels: orderbook levels (price, qty).
            notional_usd: trade size in USD.
            top_of_book_price: best bid (sell) or best ask (buy).

        Returns:
            Slippage in basis points.  Positive = worse than top-of-book.
        """
        if top_of_book_price <= 0 or notional_usd <= 0:
            return 0.0

        avg_price = SlippageModel.walk_book(levels, notional_usd)
        if avg_price <= 0:
            return 1000.0  # no depth — max penalty

        return abs(avg_price - top_of_book_price) / top_of_book_price * 10_000
