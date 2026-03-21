from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List

from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent


@dataclass
class StrategyContext:
    """Per-strategy bookkeeping (cooldowns, state, etc.)."""
    cooldown_until: Dict[str, float] = field(default_factory=dict)


class StockBaseStrategy(ABC):
    """Base class for all stock trading strategies."""

    def __init__(self, strategy_id: StockStrategyId) -> None:
        self._strategy_id = strategy_id
        self._ctx = StrategyContext()

    @property
    def strategy_id(self) -> StockStrategyId:
        return self._strategy_id

    @property
    def default_mode(self) -> str:
        """Override to return 'semi_auto' for higher-risk strategies."""
        return "auto"

    @abstractmethod
    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        """Analyse snapshot, return trade intents (empty list = no signal)."""
        ...
