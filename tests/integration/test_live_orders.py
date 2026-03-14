import asyncio
import os
import pytest

from arbitrage.exchanges import OKXRestClient, BybitRestClient
from arbitrage.utils import ExchangeConfig


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS", "false").lower() not in {"1", "true", "yes"},
    reason="integration tests disabled",
)


def _okx():
    return OKXRestClient(
        ExchangeConfig(
            api_key=os.getenv("OKX_API_KEY", ""),
            api_secret=os.getenv("OKX_API_SECRET", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            testnet=os.getenv("OKX_TESTNET", "false").lower() == "true",
        )
    )


def _bybit():
    return BybitRestClient(
        ExchangeConfig(
            api_key=os.getenv("BYBIT_API_KEY", ""),
            api_secret=os.getenv("BYBIT_API_SECRET", ""),
            passphrase="",
            testnet=os.getenv("BYBIT_TESTNET", "false").lower() == "true",
        )
    )


@pytest.mark.skipif(
    os.getenv("INTEGRATION_PLACE_ORDERS", "false").lower() not in {"1", "true", "yes"},
    reason="order placement disabled",
)
def test_place_and_cancel_okx():
    client = _okx()
    symbol = os.getenv("INTEGRATION_SYMBOL", "BTCUSDT")
    qty = float(os.getenv("INTEGRATION_QTY", "1"))
    data = asyncio.run(client.place_order(symbol=symbol, side="buy", size=qty, order_type="limit", price=1.0, time_in_force="gtc"))
    assert data
    ord_id = data.get("data", [{}])[0].get("ordId")
    if ord_id:
        cancel = asyncio.run(client.cancel_order(symbol, ord_id))
        assert cancel is not None


@pytest.mark.skipif(
    os.getenv("INTEGRATION_PLACE_ORDERS", "false").lower() not in {"1", "true", "yes"},
    reason="order placement disabled",
)
def test_place_and_cancel_bybit():
    client = _bybit()
    symbol = os.getenv("INTEGRATION_SYMBOL", "BTCUSDT")
    qty = float(os.getenv("INTEGRATION_QTY", "1"))
    data = asyncio.run(client.place_order(symbol=symbol, side="buy", size=qty, order_type="limit", price=1.0, time_in_force="GTC"))
    assert data
    ord_id = data.get("result", {}).get("orderId")
    if ord_id:
        cancel = asyncio.run(client.cancel_order(symbol, ord_id))
        assert cancel is not None
