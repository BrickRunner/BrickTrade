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
        weights: Dict[StrategyId, float] = {strategy: 1.0 for strategy in enabled}

        if StrategyId.FUNDING_ARBITRAGE in weights:
            weights[StrategyId.FUNDING_ARBITRAGE] += max(0.0, avg_funding_bps / 4.0)
        if StrategyId.FUNDING_SPREAD in weights:
            weights[StrategyId.FUNDING_SPREAD] += max(0.0, avg_funding_bps / 6.0)
        if StrategyId.GRID in weights and volatility_regime < 1.0:
            weights[StrategyId.GRID] += (1.0 - volatility_regime) * 2.0
        if StrategyId.INDICATOR in weights and trend_strength > 0:
            weights[StrategyId.INDICATOR] += trend_strength * 2.5
        if StrategyId.CASH_CARRY in weights:
            weights[StrategyId.CASH_CARRY] += 0.5
        if StrategyId.SPOT_ARBITRAGE in weights:
            weights[StrategyId.SPOT_ARBITRAGE] += 0.5

        weight_sum = sum(weights.values()) or 1.0
        per_strategy_hard_cap = equity * self.risk_config.max_strategy_allocation_pct
        allocations: Dict[StrategyId, float] = {}
        for strategy, weight in weights.items():
            target = allocatable * (weight / weight_sum)
            allocations[strategy] = min(target, per_strategy_hard_cap)

        return AllocationPlan(strategy_allocations=allocations, total_allocatable_capital=allocatable)
