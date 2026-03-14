import asyncio
import os
import pytest

from arbitrage.core.market_data import MarketDataEngine
from arbitrage.exchanges import OKXRestClient, BybitRestClient
from arbitrage.utils import ExchangeConfig


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS", "false").lower() not in {"1", "true", "yes"},
    reason="integration tests disabled",
)


def _clients():
    okx = OKXRestClient(
        ExchangeConfig(
            api_key=os.getenv("OKX_API_KEY", ""),
            api_secret=os.getenv("OKX_API_SECRET", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            testnet=os.getenv("OKX_TESTNET", "false").lower() == "true",
        )
    )
    bybit = BybitRestClient(
        ExchangeConfig(
            api_key=os.getenv("BYBIT_API_KEY", ""),
            api_secret=os.getenv("BYBIT_API_SECRET", ""),
            passphrase="",
            testnet=os.getenv("BYBIT_TESTNET", "false").lower() == "true",
        )
    )
    return {"okx": okx, "bybit": bybit}


def test_fetch_fees_and_instruments():
    clients = _clients()
    engine = MarketDataEngine(clients)
    asyncio.run(engine.initialize())
    asyncio.run(engine.update_fee_rates())
    fees = engine.get_fee_bps()
    assert "okx" in fees or "bybit" in fees
