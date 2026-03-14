from __future__ import annotations

from dataclasses import dataclass


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
