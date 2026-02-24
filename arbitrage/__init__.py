"""
Cross-Exchange Perpetual Futures Arbitrage Bot
OKX <-> HTX

Профессиональный торговый бот для арбитража между фьючерсами OKX и HTX
"""

__version__ = "1.0.0"
__author__ = "Arbitrage Bot Team"

from .core import (
    BotState,
    RiskManager,
    ExecutionManager,
    ArbitrageEngine
)

from .exchanges import (
    OKXWebSocket,
    OKXRestClient,
    HTXWebSocket,
    HTXRestClient
)

from .utils import (
    ArbitrageConfig,
    init_arbitrage_logger,
    get_arbitrage_logger
)

__all__ = [
    'BotState',
    'RiskManager',
    'ExecutionManager',
    'ArbitrageEngine',
    'OKXWebSocket',
    'OKXRestClient',
    'HTXWebSocket',
    'HTXRestClient',
    'ArbitrageConfig',
    'init_arbitrage_logger',
    'get_arbitrage_logger'
]
