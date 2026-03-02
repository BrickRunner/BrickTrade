"""
Base classes for all arbitrage strategies.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Tuple
from enum import Enum
import time


class StrategyType(Enum):
    FUNDING_ARB = "funding_arb"
    BASIS_ARB = "basis_arb"
    STAT_ARB = "stat_arb"

    @property
    def display_name(self) -> str:
        names = {
            "funding_arb": "Funding Rate",
            "basis_arb": "Basis (Cash&Carry)",
            "stat_arb": "Statistical Arb",
        }
        return names.get(self.value, self.value)

    @property
    def emoji(self) -> str:
        emojis = {
            "funding_arb": "💸",
            "basis_arb": "⚖️",
            "stat_arb": "📊",
        }
        return emojis.get(self.value, "📈")


@dataclass
class Opportunity:
    """A detected arbitrage opportunity ready for execution."""
    strategy: StrategyType
    symbol: str
    long_exchange: str
    short_exchange: str
    expected_profit_pct: float
    long_price: float = 0.0
    short_price: float = 0.0
    confidence: float = 1.0
    # Extra data per strategy
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def spread(self) -> float:
        if self.long_price > 0 and self.short_price > 0:
            return (self.short_price - self.long_price) / self.long_price * 100
        return 0.0


class BaseStrategy(ABC):
    """Abstract base class for all arbitrage strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name identifier."""
        ...

    @property
    @abstractmethod
    def strategy_type(self) -> StrategyType:
        """Strategy type enum."""
        ...

    @abstractmethod
    async def detect_opportunities(self, market_data) -> List[Opportunity]:
        """
        Analyze market data and return a list of trade opportunities.
        Called every cycle by the router.
        """
        ...

    @abstractmethod
    async def should_exit(self, position, market_data) -> Tuple[bool, str]:
        """
        Check if an existing position should be closed.
        Returns (should_close, reason).
        """
        ...

    @abstractmethod
    def get_threshold(self, symbol: str) -> float:
        """
        Get the entry threshold for a given symbol.
        May vary per asset (BTC vs ALT).
        """
        ...
