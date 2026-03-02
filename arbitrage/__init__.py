"""
Cross-Exchange Perpetual Futures Arbitrage Bot
OKX <-> HTX <-> Bybit

Professional multi-strategy arbitrage trading system.
"""

__version__ = "2.0.0"

from .core import (
    BotState,
    ActivePosition,
    RiskManager,
    NotificationManager,
    MarketDataEngine,
)

from .exchanges import (
    OKXRestClient,
    HTXRestClient,
    BybitRestClient,
)

from .utils import (
    ArbitrageConfig,
    init_arbitrage_logger,
    get_arbitrage_logger,
)

from .strategies import (
    StrategyRouter,
    TradeExecutor,
)

__all__ = [
    'BotState',
    'ActivePosition',
    'RiskManager',
    'NotificationManager',
    'MarketDataEngine',
    'OKXRestClient',
    'HTXRestClient',
    'BybitRestClient',
    'ArbitrageConfig',
    'init_arbitrage_logger',
    'get_arbitrage_logger',
    'StrategyRouter',
    'TradeExecutor',
]
