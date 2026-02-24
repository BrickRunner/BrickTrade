"""
Арбитражные стратегии
"""
from arbitrage.strategies.base import (
    StrategyType,
    BaseOpportunity,
    SpotArbitrageOpportunity,
    FuturesArbitrageOpportunity,
    FundingArbitrageOpportunity,
    BasisArbitrageOpportunity,
    TriangularArbitrageOpportunity,
)
from arbitrage.strategies.spot_arb import SpotArbitrageMonitor
from arbitrage.strategies.futures_arb import FuturesArbitrageMonitor
from arbitrage.strategies.funding_arb import FundingArbitrageMonitor
from arbitrage.strategies.basis_arb import BasisArbitrageMonitor
from arbitrage.strategies.triangular_arb import TriangularArbitrageMonitor
from arbitrage.strategies.trade_executor import TradeExecutor, TradeRecord
from arbitrage.strategies.strategy_manager import StrategyManager

__all__ = [
    "StrategyType",
    "BaseOpportunity",
    "SpotArbitrageOpportunity",
    "FuturesArbitrageOpportunity",
    "FundingArbitrageOpportunity",
    "BasisArbitrageOpportunity",
    "TriangularArbitrageOpportunity",
    "SpotArbitrageMonitor",
    "FuturesArbitrageMonitor",
    "FundingArbitrageMonitor",
    "BasisArbitrageMonitor",
    "TriangularArbitrageMonitor",
    "TradeExecutor",
    "TradeRecord",
    "StrategyManager",
]
