from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from arbitrage.system.config import RiskConfig
from arbitrage.system.models import AllocationPlan, StrategyId


@dataclass
class CapitalAllocator:
    risk_config: RiskConfig

    def allocate(
        self,
        equity: float,
        avg_funding_bps: float,
        volatility_regime: float,
        trend_strength: float,
        enabled: list[StrategyId],
    ) -> AllocationPlan:
        allocatable = equity * self.risk_config.max_total_exposure_pct

        # --- Dynamic weighting based on market conditions ---
        weights: Dict[StrategyId, float] = {}
        for strategy in enabled:
            w = 1.0

            # Volatility scaling: reduce allocation in extreme volatility,
            # increase slightly in moderate volatility (best for arb).
            # vol_regime is typically 0.0001 - 0.01 (0.01% - 1% per tick).
            if volatility_regime > 0.005:
                # High volatility — scale down to reduce risk
                w *= 0.5
            elif volatility_regime > 0.002:
                # Moderate volatility — optimal for arb, full allocation
                w *= 1.0
            else:
                # Very low volatility — fewer opportunities, slight reduction
                w *= 0.8

            # Funding rate boost: higher funding spread means more arb
            # opportunity.  avg_funding_bps is the max funding in bps.
            if avg_funding_bps > 15.0:
                w *= 1.3  # strong funding differential
            elif avg_funding_bps > 5.0:
                w *= 1.1  # moderate

            # Trend penalty: strong trending markets increase one-sided risk
            # for delta-neutral strategies.  trend_strength is the fractional
            # price change over the observation window.
            abs_trend = abs(trend_strength)
            if abs_trend > 0.02:
                # Strong trend — reduce allocation
                w *= 0.6
            elif abs_trend > 0.01:
                w *= 0.8

            weights[strategy] = max(0.1, w)  # floor at 10%

        # When only one strategy is enabled, the weight directly scales
        # the allocatable amount (no peer normalization needed).
        per_strategy_hard_cap = equity * self.risk_config.max_strategy_allocation_pct
        allocations: Dict[StrategyId, float] = {}
        if len(weights) == 1:
            strategy, weight = next(iter(weights.items()))
            allocations[strategy] = min(allocatable * weight, per_strategy_hard_cap)
        else:
            weight_sum = sum(weights.values()) or 1.0
            for strategy, weight in weights.items():
                target = allocatable * (weight / weight_sum)
                allocations[strategy] = min(target, per_strategy_hard_cap)

        return AllocationPlan(strategy_allocations=allocations, total_allocatable_capital=allocatable)
