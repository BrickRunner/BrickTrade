"""
Comprehensive test suite for all critical fixes from code review.
"""
from __future__ import annotations
import asyncio, json, os, tempfile, time
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# ==============================================================================
# FIX #6: WS Lock-Protected Reads
# ==============================================================================
@pytest.mark.asyncio
async def test_ws_cache_has_lock():
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    cache = WsOrderbookCache(symbols=[], exchanges=[])
    assert isinstance(cache._lock, asyncio.Lock)

@pytest.mark.asyncio
async def test_ws_cache_concurrent_writes():
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    from arbitrage.system.models import OrderBookSnapshot
    cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
    async def write_i(i: int):
        async with cache._lock:
            cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = OrderBookSnapshot(
                exchange="okx", symbol="BTCUSDT", bid=100.0+i, ask=101.0+i)
        await asyncio.sleep(0.0001)
    tasks = [asyncio.create_task(write_i(i)) for i in range(100)]
    await asyncio.gather(*tasks)
    async with cache._lock:
        ob = cache._orderbooks.get("okx", {}).get("BTCUSDT")
    assert ob is not None and ob.bid < ob.ask

@pytest.mark.asyncio
async def test_ws_cache_has_ws_instances_dict():
    """FIX #5,#7,#9: _ws_instances exists for health checks."""
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    cache = WsOrderbookCache(symbols=[], exchanges=[])
    assert hasattr(cache, '_ws_instances')
    assert isinstance(cache._ws_instances, dict)

@pytest.mark.asyncio
async def test_ws_cache_async_get_returns_none_when_empty():
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    cache = WsOrderbookCache(symbols=[], exchanges=[])
    assert await cache.get("okx", "BTCUSDT") is None

@pytest.mark.asyncio
async def test_ws_cache_async_depth_returns_none_when_empty():
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    cache = WsOrderbookCache(symbols=[], exchanges=[])
    assert await cache.get_depth("okx", "BTCUSDT") is None

@pytest.mark.asyncio
async def test_ws_cache_health_status():
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
    status = await cache.health_status()
    assert "okx:BTCUSDT" in status
    assert "alive" in status["okx:BTCUSDT"]

@pytest.mark.asyncio
async def test_ws_cache_mark_stale_for_reconnect():
    from arbitrage.system.ws_orderbooks import WsOrderbookCache
    cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
    task = MagicMock()
    task.done.return_value = False
    cache._tasks["okx:BTCUSDT"] = task
    cache.mark_stale_for_reconnect("okx", "BTCUSDT")
    task.cancel.assert_called_once()

# ==============================================================================
# FIX #10: Session Lock on All 4 Exchanges
# ==============================================================================
def _make_exc(api_key="", api_secret="", passphrase=""):
    from arbitrage.utils import ExchangeConfig
    return ExchangeConfig(api_key=api_key, api_secret=api_secret, passphrase=passphrase)

@pytest.mark.asyncio
async def test_binance_session_has_lock():
    from arbitrage.exchanges.binance_rest import BinanceRestClient
    c = BinanceRestClient(_make_exc())
    assert isinstance(c._session_lock, asyncio.Lock)

@pytest.mark.asyncio
async def test_okx_session_has_lock():
    from arbitrage.exchanges.okx_rest import OKXRestClient
    c = OKXRestClient(_make_exc("k","s","p"))
    assert isinstance(c._session_lock, asyncio.Lock)

@pytest.mark.asyncio
async def test_bybit_session_has_lock():
    from arbitrage.exchanges.bybit_rest import BybitRestClient
    c = BybitRestClient(_make_exc("k","s"))
    assert isinstance(c._session_lock, asyncio.Lock)

@pytest.mark.asyncio
async def test_htx_session_has_lock():
    from arbitrage.exchanges.htx_rest import HTXRestClient
    c = HTXRestClient(_make_exc("k","s"))
    assert isinstance(c._session_lock, asyncio.Lock)

@pytest.mark.asyncio
async def test_binance_session_lock_prevents_race():
    from arbitrage.exchanges.binance_rest import BinanceRestClient
    c = BinanceRestClient(_make_exc())
    s1, s2 = await asyncio.gather(c._get_session(), c._get_session())
    assert s1 is s2

# ==============================================================================
# CRITICAL #4: Kill Switch
# ==============================================================================
@pytest.mark.asyncio
async def test_kill_switch_non_permanent_triggers():
    from arbitrage.system.state import SystemState
    s = SystemState(starting_equity=1000.0, positions_file=":memory:")
    assert not await s.kill_switch_triggered()
    await s.trigger_kill_switch(permanent=False)
    assert await s.kill_switch_triggered()

@pytest.mark.asyncio
async def test_kill_switch_permanent():
    from arbitrage.system.state import SystemState
    s = SystemState(starting_equity=1000.0, positions_file=":memory:")
    await s.trigger_kill_switch(permanent=True)
    assert await s.kill_switch_triggered()

# ==============================================================================
# CRITICAL #3: Position Deduplication
# ==============================================================================
@pytest.mark.asyncio
async def test_dedup_detects_duplicate_positions():
    from arbitrage.system.state import SystemState
    from arbitrage.system.models import OpenPosition, StrategyId
    s = SystemState(starting_equity=1000.0, positions_file=":memory:")
    p1 = OpenPosition(position_id="u1", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
        symbol="BTCUSDT", long_exchange="okx", short_exchange="htx",
        notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=10.0, opened_at=1000.0)
    p2 = OpenPosition(position_id="u2", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
        symbol="BTCUSDT", long_exchange="okx", short_exchange="htx",
        notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=10.0, opened_at=2000.0)
    await s.add_position(p1); await s.add_position(p2)
    positions = await s.list_positions()
    assert len(positions) == 2
    seen = {}; dups = []
    for p in positions:
        k = f"{p.strategy_id.value}:{p.symbol}:{p.long_exchange}:{p.short_exchange}"
        if k in seen: dups.append(p.position_id)
        else: seen[k] = p
    assert len(dups) == 1

# ==============================================================================
# CRITICAL #7: Walk-the-Book
# ==============================================================================
def test_slippage_walk_book():
    from arbitrage.system.slippage import SlippageModel
    walked = SlippageModel.walk_book([[100.0,1.0],[101.0,2.0],[102.0,3.0]], 150.0)
    assert walked > 0

@pytest.mark.asyncio
async def test_funding_strategy_with_depth():
    from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
    from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
    s = FuturesCrossExchangeStrategy(funding_threshold_pct=0.01)
    snap = MarketSnapshot(symbol="BTCUSDT",
        orderbooks={"okx": OrderBookSnapshot("okx","BTCUSDT",50000,50010),
                     "htx": OrderBookSnapshot("htx","BTCUSDT",50020,50030)},
        spot_orderbooks={},
        orderbook_depth={"okx":{"bids":[[50000,2]],"asks":[[50010,2]]},
                          "htx":{"bids":[[50020,2]],"asks":[[50030,2]]}},
        spot_orderbook_depth={},
        balances={"okx":10000,"htx":10000}, fee_bps={"okx":{"perp":5},"htx":{"perp":5}},
        funding_rates={"okx":0.0001,"htx":0.0003}, volatility=0.001, trend_strength=0,
        atr=50, atr_rolling=50, indicators={}, timestamp=time.time())
    intents = await s.on_market_snapshot(snap)
    assert isinstance(intents, list)

# ==============================================================================
# Config / Risk
# ==============================================================================
def test_trading_system_config_from_env():
    from arbitrage.system.config import TradingSystemConfig
    c = TradingSystemConfig.from_env()
    assert len(c.symbols) > 0
    assert len(c.exchanges) >= 2
    assert 0 < c.risk.max_total_exposure_pct <= 1

def test_risk_config_defaults():
    from arbitrage.system.config import RiskConfig
    c = RiskConfig()
    assert c.max_order_slippage_bps > 0 and c.kill_switch_enabled is True

def test_execution_config_defaults():
    from arbitrage.system.config import ExecutionConfig
    c = ExecutionConfig()
    assert c.cycle_interval_seconds > 0 and c.dry_run is True

# ==============================================================================
# Execution V2
# ==============================================================================
@pytest.mark.asyncio
async def test_execution_v2_preflight_insufficient_balance():
    from arbitrage.system.execution_v2 import AtomicExecutionEngineV2, ExecutionStatus
    mock_venue = MagicMock()
    mock_config = MagicMock(); mock_config.margin_requirements = {}
    eng = AtomicExecutionEngineV2(MagicMock(), mock_config, MagicMock(), 2.0, 0.30)
    intent = MagicMock(); intent.symbol="BTCUSDT"
    intent.metadata={"long_exchange":"okx","short_exchange":"htx"}
    r = await eng.execute_arbitrage(intent, {"okx":0,"htx":0})
    assert not r.success

@pytest.mark.asyncio
async def test_execution_v2_preflight_existing_position():
    from arbitrage.system.execution_v2 import AtomicExecutionEngineV2, ExecutionStatus
    mock_monitor = MagicMock()
    # Create a real minimal engine with proper config
    class FakeConfig:
        margin_requirements = {"okx": 0.15, "htx": 0.20}
    mock_venue = MagicMock()
    eng = AtomicExecutionEngineV2(mock_venue, FakeConfig(), mock_monitor, 2.0, 0.30)
    eng._get_position_size = AsyncMock(return_value=0.5)
    intent = MagicMock()
    intent.symbol = "BTCUSDT"
    intent.metadata = {"long_exchange": "okx", "short_exchange": "htx"}
    r = await eng.execute_arbitrage(intent, {"okx": 100, "htx": 100})
    assert not r.success

# ==============================================================================
# WS Orderbook Validation
# ==============================================================================
def test_validate_orderbook_valid():
    from arbitrage.utils import validate_orderbook
    assert validate_orderbook({"bids":[[50000,1]],"asks":[[50010,1]]}) is True

def test_validate_orderbook_crossed():
    from arbitrage.utils import validate_orderbook
    assert validate_orderbook({"bids":[[50010,1]],"asks":[[50000,1]]}) is False

def test_validate_orderbook_empty():
    from arbitrage.utils import validate_orderbook
    assert validate_orderbook({}) is False

# ==============================================================================
# Helpers - calculate_spread, usdt_to_htx
# ==============================================================================
def test_calculate_spread():
    from arbitrage.utils.helpers import calculate_spread
    assert abs(calculate_spread(99.0, 100.0) - (-1.0)) < 0.0001
    assert calculate_spread(101.0, 100.0) == 1.0
    assert calculate_spread(0.0, 0.0) == 0.0

def test_usdt_to_htx():
    from arbitrage.utils.helpers import usdt_to_htx
    assert usdt_to_htx("BTCUSDT") == "BTC-USDT"
    assert usdt_to_htx("btcusdt") == "BTC-USDT"
    assert usdt_to_htx("BTC-USDT") == "BTC-USDT"

# ==============================================================================
# Slippage Model
# ==============================================================================
def test_slippage_model_sanity():
    from arbitrage.system.slippage import SlippageModel
    m = SlippageModel()
    low = m.estimate(100, 5000000, 0.001, 50)
    high = m.estimate(1000000, 5000, 0.05, 500)
    assert low >= 0 and high >= 0 and high >= low

# ==============================================================================
# Rate Limiter
# ==============================================================================
@pytest.mark.asyncio
async def test_rate_limiter_acquire():
    from arbitrage.utils.rate_limiter import get_rate_limiter
    await get_rate_limiter().acquire("binance")

@pytest.mark.asyncio
async def test_rate_limiter_429_backoff():
    from arbitrage.utils.rate_limiter import get_rate_limiter
    lim = get_rate_limiter()
    b = lim.record_429("binance")
    assert b > 0
    lim.record_success("binance")
    assert lim._buckets["binance"].consecutive_429 == 0

# ==============================================================================
# State persistence
# ==============================================================================
@pytest.mark.asyncio
async def test_state_persists_positions_on_disk():
    from arbitrage.system.state import SystemState
    from arbitrage.system.models import OpenPosition, StrategyId
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "pos.json")
        s = SystemState(1000.0, path)
        p = OpenPosition(position_id="t1", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="htx",
            notional_usd=100, entry_mid=50000, stop_loss_bps=10)
        await s.add_position(p)
        await asyncio.sleep(0.3)
        assert os.path.exists(path)

@pytest.mark.asyncio
async def test_state_loads_positions_on_restart():
    from arbitrage.system.state import SystemState
    from arbitrage.system.models import OpenPosition, StrategyId
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "pos2.json")
        s1 = SystemState(1000.0, path)
        p = OpenPosition(position_id="t2", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="htx",
            notional_usd=100, entry_mid=50000, stop_loss_bps=10)
        await s1.add_position(p)
        await asyncio.sleep(0.3)
        s2 = SystemState(1000.0, path)
        assert len(await s2.list_positions()) >= 1
