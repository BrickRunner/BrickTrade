from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent


@dataclass
class StrategyContext:
    cooldown_by_symbol: Dict[str, float] = field(default_factory=dict)


class BaseStrategy(ABC):
    def __init__(self, strategy_id: StrategyId):
        self._strategy_id = strategy_id
        self._context = StrategyContext()

    @property
    def strategy_id(self) -> StrategyId:
        return self._strategy_id

    @abstractmethod
    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        ...
