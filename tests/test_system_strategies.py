from __future__ import annotations

import pytest

from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
from arbitrage.system.strategies.cash_carry import CashCarryStrategy
from arbitrage.system.strategies.funding_arbitrage import FundingArbitrageStrategy
from arbitrage.system.strategies.funding_spread import FundingSpreadStrategy
from arbitrage.system.strategies.grid import GridStrategy
from arbitrage.system.strategies.indicator import IndicatorStrategy
from arbitrage.system.strategies.spot_arbitrage import SpotArbitrageStrategy


def build_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        orderbooks={
            "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=100.0, ask=100.2),
            "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=101.0, ask=101.2),
        },
        spot_orderbooks={
            "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=99.8, ask=100.1),
            "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=100.6, ask=101.0),
        },
        orderbook_depth={},
        spot_orderbook_depth={},
        balances={"okx": 1000.0, "htx": 1000.0},
        fee_bps={"okx": {"spot": 6.0, "perp": 6.0}, "htx": {"spot": 6.0, "perp": 6.0}},
        funding_rates={"okx": -0.0001, "htx": 0.0002},
        volatility=0.15,
        trend_strength=0.4,
        atr=20.0,
        atr_rolling=25.0,
        indicators={
            "rsi": 30.0,
            "ema_fast": 101.0,
            "ema_slow": 99.0,
            "vwap": 100.0,
            "bb_upper": 102.0,
            "bb_lower": 100.6,
            "spot_price": 100.0,
            "perp_price": 100.25,
        },
    )


@pytest.mark.asyncio
async def test_spot_arbitrage_signal():
    snapshot = build_snapshot()
    strategy = SpotArbitrageStrategy(min_edge_bps=5.0, fee_bps=2.0, slippage_buffer_bps=1.0)
    intents = await strategy.on_market_snapshot(snapshot)
    assert intents
    assert intents[0].symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_cash_carry_signal():
    snapshot = build_snapshot()
    strategy = CashCarryStrategy(basis_threshold_bps=10.0, safety_margin_bps=2.0)
    intents = await strategy.on_market_snapshot(snapshot)
    assert len(intents) == 1


@pytest.mark.asyncio
async def test_funding_arbitrage_signal():
    snapshot = build_snapshot()
    strategy = FundingArbitrageStrategy(funding_threshold_bps=1.0)
    intents = await strategy.on_market_snapshot(snapshot)
    assert len(intents) == 1
    assert intents[0].long_exchange == "okx"
    assert intents[0].short_exchange == "htx"


@pytest.mark.asyncio
async def test_funding_spread_signal():
    snapshot = build_snapshot()
    strategy = FundingSpreadStrategy(threshold_bps=1.0)
    intents = await strategy.on_market_snapshot(snapshot)
    assert intents


@pytest.mark.asyncio
async def test_grid_signal():
    snapshot = build_snapshot()
    strategy = GridStrategy(max_breakout_ratio=1.2)
    intents = await strategy.on_market_snapshot(snapshot)
    assert len(intents) == 1


@pytest.mark.asyncio
async def test_indicator_signal():
    snapshot = build_snapshot()
    strategy = IndicatorStrategy()
    intents = await strategy.on_market_snapshot(snapshot)
    assert len(intents) == 1
