"""Shared factory functions for building exchange clients and the trading engine."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger("trading_system")

from arbitrage.core.market_data import MarketDataEngine
from arbitrage.exchanges import BinanceRestClient, BybitRestClient, HTXRestClient, OKXRestClient
from arbitrage.exchanges.private_ws import PrivateWsManager
from arbitrage.system.config import TradingSystemConfig
from arbitrage.utils import ExchangeConfig


_CLIENT_CLASSES = {
    "okx": OKXRestClient,
    "htx": HTXRestClient,
    "bybit": BybitRestClient,
    "binance": BinanceRestClient,
}


def build_exchange_clients(
    config: TradingSystemConfig,
    *,
    validate_credentials: bool = True,
) -> Dict[str, Any]:
    """Build REST clients for all configured exchanges.

    Args:
        config: Trading system configuration with exchange list & credentials.
        validate_credentials: If True, raise on missing API keys in live mode.

    Returns:
        Mapping of exchange name to REST client instance.

    Raises:
        ValueError: If fewer than 2 clients can be created, or credentials
            are missing in live mode (when *validate_credentials* is True).
    """
    clients: Dict[str, Any] = {}
    for exchange in config.exchanges:
        creds = config.credentials.get(exchange)
        if not creds:
            continue

        if validate_credentials and not config.execution.dry_run and exchange in {"okx", "htx", "binance"}:
            if not creds.api_key or not creds.api_secret:
                raise ValueError(f"Missing API credentials for {exchange} in live mode")
            if exchange == "okx" and not creds.passphrase:
                raise ValueError("Missing OKX passphrase in live mode")

        cls = _CLIENT_CLASSES.get(exchange)
        if cls is None:
            continue

        ex_cfg = ExchangeConfig(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            passphrase=creds.passphrase,
            testnet=os.getenv(f"{exchange.upper()}_TESTNET", "false").lower() == "true",
        )
        clients[exchange] = cls(ex_cfg)

    if len(clients) < 2:
        raise ValueError("At least two exchange clients are required")
    return clients


def build_private_ws_manager(config: TradingSystemConfig) -> PrivateWsManager:
    """Build PrivateWsManager with configs for all exchanges that have API keys.

    Provides real-time WS push for balances, order fills, and positions
    instead of REST polling.
    """
    configs: Dict[str, ExchangeConfig] = {}
    for exchange in config.exchanges:
        creds = config.credentials.get(exchange)
        if not creds or not creds.api_key or not creds.api_secret:
            continue
        # Private WS supported for okx, htx, bybit (not binance yet)
        if exchange not in {"okx", "htx", "bybit"}:
            continue
        configs[exchange] = ExchangeConfig(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            passphrase=creds.passphrase,
            testnet=os.getenv(f"{exchange.upper()}_TESTNET", "false").lower() == "true",
        )
    return PrivateWsManager(configs=configs)


def usdt_symbol_universe(
    market_data: MarketDataEngine,
    max_symbols: int = 30,
    blacklist: list[str] | None = None,
) -> list[str]:
    """Return sorted USDT-margined common pairs, excluding blacklisted, capped at *max_symbols*."""
    bl = set(blacklist or [])
    universe = sorted(
        s for s in market_data.common_pairs
        if s.endswith("USDT") and s not in bl
    )
    if bl:
        logger.info("symbol_universe: blacklisted %d pairs: %s", len(bl), ", ".join(sorted(bl)))
    return universe[:max(1, max_symbols)]
