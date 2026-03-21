from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Set

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


@dataclass
class StockStrategyRunner:
    """Runs multiple stock strategies in parallel on a given snapshot."""

    strategies: List[StockBaseStrategy]

    async def generate_intents(
        self,
        snapshot: StockSnapshot,
        enabled_ids: Optional[Set[StockStrategyId]] = None,
    ) -> List[StockTradeIntent]:
        filtered = self.strategies
        if enabled_ids is not None:
            filtered = [s for s in self.strategies if s.strategy_id in enabled_ids]

        tasks = [
            asyncio.create_task(self._run_one(s, snapshot)) for s in filtered
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        intents: List[StockTradeIntent] = []
        for result in results:
            if isinstance(result, list):
                intents.extend(result)
            elif isinstance(result, BaseException):
                logger.error("strategy_runner: strategy error: %s", result)
        return intents

    @staticmethod
    async def _run_one(
        strategy: StockBaseStrategy, snapshot: StockSnapshot
    ) -> List[StockTradeIntent]:
        return await strategy.on_snapshot(snapshot)
