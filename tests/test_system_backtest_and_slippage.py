from __future__ import annotations

import logging

import pytest

from arbitrage.system.backtest.engine import BacktestEngine
from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.config import ExecutionConfig, RiskConfig
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
from arbitrage.system.monitoring import InMemoryMonitoring
from arbitrage.system.risk import RiskEngine
from arbitrage.system.simulation.exchange import SimulatedExecutionVenue
from arbitrage.system.simulation.market_data import ReplayMarketDataProvider
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState
from arbitrage.system.strategy_runner import StrategyRunner
from arbitrage.system.strategies.spot_arbitrage import SpotArbitrageStrategy


def _frame(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        orderbooks={
            "okx": OrderBookSnapshot(exchange="okx", symbol=symbol, bid=100.0, ask=100.1),
            "htx": OrderBookSnapshot(exchange="htx", symbol=symbol, bid=100.8, ask=100.9),
        },
        spot_orderbooks={
            "okx": OrderBookSnapshot(exchange="okx", symbol=symbol, bid=99.9, ask=100.05),
            "htx": OrderBookSnapshot(exchange="htx", symbol=symbol, bid=100.6, ask=100.85),
        },
        orderbook_depth={},
        spot_orderbook_depth={},
        balances={"okx": 1000.0, "htx": 1000.0},
        fee_bps={"okx": {"spot": 6.0, "perp": 6.0}, "htx": {"spot": 6.0, "perp": 6.0}},
        funding_rates={"okx": -0.0001, "htx": 0.0001},
        volatility=0.2,
        trend_strength=0.2,
        atr=10.0,
        atr_rolling=12.0,
        indicators={"rsi": 50.0, "ema_fast": 100.0, "ema_slow": 99.0, "vwap": 100.0, "bb_upper": 101.0, "bb_lower": 99.0},
    )


def test_slippage_model_increases_with_latency_and_size():
    model = SlippageModel()
    low = model.estimate(100, 1_000_000, 0.05, 20)
    high = model.estimate(100_000, 200_000, 0.4, 500)
    assert high > low


@pytest.mark.asyncio
async def test_backtest_engine_runs_and_executes():
    state = SystemState(10_000)
    monitor = InMemoryMonitoring(logging.getLogger("test"))
    venue = SimulatedExecutionVenue()
    execution = AtomicExecutionEngine(ExecutionConfig(dry_run=True), venue, SlippageModel(), state, monitor)
    risk = RiskEngine(RiskConfig(), state)
    allocator = CapitalAllocator(RiskConfig())
    runner = StrategyRunner([SpotArbitrageStrategy(min_edge_bps=2.0)], monitor)
    backtest = BacktestEngine(["BTCUSDT"], runner, risk, allocator, execution)
    provider = ReplayMarketDataProvider({"BTCUSDT": [_frame("BTCUSDT"), _frame("BTCUSDT")]})
    result = await backtest.run(provider)
    assert result.trades >= 1
    assert result.accepted >= 1
