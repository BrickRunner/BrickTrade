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

        weight_sum = sum(weights.values()) or 1.0
        per_strategy_hard_cap = equity * self.risk_config.max_strategy_allocation_pct
        allocations: Dict[StrategyId, float] = {}
        for strategy, weight in weights.items():
            target = allocatable * (weight / weight_sum)
            allocations[strategy] = min(target, per_strategy_hard_cap)

        return AllocationPlan(strategy_allocations=allocations, total_allocatable_capital=allocatable)
