"""Shared factory functions for building exchange clients and the trading engine."""
from __future__ import annotations

import os
from typing import Any, Dict

from arbitrage.core.market_data import MarketDataEngine
from arbitrage.exchanges import BybitRestClient, HTXRestClient, OKXRestClient
from arbitrage.system.config import TradingSystemConfig
from arbitrage.utils import ExchangeConfig


_CLIENT_CLASSES = {
    "okx": OKXRestClient,
    "htx": HTXRestClient,
    "bybit": BybitRestClient,
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

        if validate_credentials and not config.execution.dry_run and exchange in {"okx", "htx"}:
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


def usdt_symbol_universe(market_data: MarketDataEngine, max_symbols: int = 30) -> list[str]:
    """Return sorted USDT-margined common pairs, capped at *max_symbols*."""
    universe = sorted(s for s in market_data.common_pairs if s.endswith("USDT"))
    return universe[:max(1, max_symbols)]
