import time
import asyncio

from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
from arbitrage.system.strategies.prefunded_arbitrage import PreFundedArbitrageStrategy
from arbitrage.system.strategies.orderbook_imbalance import OrderbookImbalanceStrategy
from arbitrage.system.strategies.spread_capture import SpreadCaptureStrategy


def _snapshot():
    orderbooks = {
        "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=101, ask=102, timestamp=time.time()),
        "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=103, ask=104, timestamp=time.time()),
    }
    spot_orderbooks = {
        "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=100, ask=102, timestamp=time.time()),
        "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=101, ask=103, timestamp=time.time()),
    }
    orderbook_depth = {
        "okx": {"bids": [[101, 10.0], [100, 8.0]], "asks": [[102, 1.0], [103, 1.0]], "timestamp": time.time()},
        "htx": {"bids": [[103, 10.0], [102, 8.0]], "asks": [[104, 1.0], [105, 1.0]], "timestamp": time.time()},
    }
    spot_orderbook_depth = {
        "okx": {"bids": [[100, 10.0], [99, 8.0]], "asks": [[102, 1.0], [103, 1.0]], "timestamp": time.time()},
        "htx": {"bids": [[101, 10.0], [100, 8.0]], "asks": [[103, 1.0], [104, 1.0]], "timestamp": time.time()},
    }
    return MarketSnapshot(
        symbol="BTCUSDT",
        orderbooks=orderbooks,
        spot_orderbooks=spot_orderbooks,
        orderbook_depth=orderbook_depth,
        spot_orderbook_depth=spot_orderbook_depth,
        balances={"okx": 1000.0, "htx": 1000.0},
        fee_bps={"okx": {"spot": 6.0, "perp": 6.0}, "htx": {"spot": 6.0, "perp": 6.0}},
        funding_rates={"okx": 0.0001, "htx": 0.0001},
        volatility=0.2,
        trend_strength=0.0,
        atr=10.0,
        atr_rolling=10.0,
        indicators={"spread_bps": 50.0, "basis_bps": 10.0, "funding_spread_bps": 0.0},
    )


def test_prefunded_arbitrage_generates_intent():
    strat = PreFundedArbitrageStrategy(min_edge_bps=5.0, min_balance_usd=1.0)
    intents = asyncio.run(strat.on_market_snapshot(_snapshot()))
    assert intents
    assert intents[0].strategy_id.value == "prefunded_arbitrage"


def test_orderbook_imbalance_generates_intent():
    strat = OrderbookImbalanceStrategy(imbalance_ratio=2.0, min_edge_bps=1.0)
    intents = asyncio.run(strat.on_market_snapshot(_snapshot()))
    assert intents
    assert intents[0].strategy_id.value == "orderbook_imbalance"


def test_spread_capture_generates_intent():
    strat = SpreadCaptureStrategy(min_spread_bps=5.0, price_improve_bps=0.5)
    intents = asyncio.run(strat.on_market_snapshot(_snapshot()))
    assert intents
    assert intents[0].strategy_id.value == "spread_capture"
