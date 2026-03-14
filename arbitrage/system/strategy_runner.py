from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Set

from arbitrage.system.interfaces import MonitoringSink
from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy


@dataclass
class StrategyRunner:
    strategies: List[BaseStrategy]
    monitor: MonitoringSink

    async def generate_intents(self, snapshot: MarketSnapshot, enabled_ids: Set[StrategyId] | None = None) -> List[TradeIntent]:
        filtered = self.strategies
        if enabled_ids is not None:
            filtered = [s for s in self.strategies if s.strategy_id in enabled_ids]
        tasks = [asyncio.create_task(self._run_strategy(strategy, snapshot)) for strategy in filtered]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        intents: List[TradeIntent] = []
        for result in results:
            if isinstance(result, list):
                intents.extend(result)
        return intents

    async def _run_strategy(self, strategy: BaseStrategy, snapshot: MarketSnapshot) -> List[TradeIntent]:
        try:
            return await strategy.on_market_snapshot(snapshot)
        except Exception as exc:
            await self.monitor.emit(
                "strategy_error",
                {"strategy": strategy.strategy_id.value, "symbol": snapshot.symbol, "error": str(exc)},
            )
            return []

    def by_id(self) -> Dict[str, BaseStrategy]:
        return {strategy.strategy_id.value: strategy for strategy in self.strategies}
