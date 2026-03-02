"""
Arbitrage strategies.
"""
from arbitrage.strategies.base import StrategyType, BaseStrategy, Opportunity
from arbitrage.strategies.funding_arb import FundingArbStrategy
from arbitrage.strategies.basis_arb import BasisArbStrategy
from arbitrage.strategies.stat_arb import StatArbStrategy
from arbitrage.strategies.trade_executor import TradeExecutor
from arbitrage.strategies.strategy_manager import StrategyRouter

__all__ = [
    "StrategyType",
    "BaseStrategy",
    "Opportunity",
    "FundingArbStrategy",
    "BasisArbStrategy",
    "StatArbStrategy",
    "TradeExecutor",
    "StrategyRouter",
]
