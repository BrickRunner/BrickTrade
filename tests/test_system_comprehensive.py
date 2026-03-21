"""
Comprehensive unit tests for the arbitrage trading system.

Covers: SystemState, RiskEngine, AtomicExecutionEngine, TradingSystemEngine,
FuturesCrossExchangeStrategy, models, helpers, and critical bug fixes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import pytest

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.config import ExecutionConfig, RiskConfig, TradingSystemConfig, StrategyConfig
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.models import (
    AllocationPlan,
    ExecutionReport,
    MarketSnapshot,
    OpenPosition,
    OrderBookSnapshot,
    RiskDecision,
    StrategyId,
    TradeIntent,
)
from arbitrage.system.monitoring import InMemoryMonitoring
from arbitrage.system.risk import RiskEngine
from arbitrage.system.simulation.exchange import SimulatedExecutionVenue
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState, _serialize_position, _deserialize_position
from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
from arbitrage.system.fees import fee_bps, fee_bps_from_snapshot
from arbitrage.utils.helpers import (
    calculate_spread,
    calculate_pnl,
    validate_orderbook,
    get_best_bid_ask,
    round_down,
    usdt_to_htx,
)


# ═══════════════════════════════════════════════════════════════
# Helpers / Fixtures
# ═══════════════════════════════════════════════════════════════

def _make_snapshot(
    symbol="BTCUSDT",
    exchanges=None,
    bid_ask_pairs=None,
    funding_rates=None,
    balances=None,
    fee_bps_map=None,
    volatility=0.001,
    trend_strength=0.0,
) -> MarketSnapshot:
    if exchanges is None:
        exchanges = ["okx", "bybit"]
    if bid_ask_pairs is None:
        bid_ask_pairs = {
            "okx": (100_000.0, 100_010.0),
            "bybit": (100_005.0, 100_015.0),
        }
    orderbooks = {}
    for ex in exchanges:
        bid, ask = bid_ask_pairs.get(ex, (100_000.0, 100_010.0))
        orderbooks[ex] = OrderBookSnapshot(exchange=ex, symbol=symbol, bid=bid, ask=ask, timestamp=time.time())
    return MarketSnapshot(
        symbol=symbol,
        orderbooks=orderbooks,
        spot_orderbooks={},
        orderbook_depth={},
        spot_orderbook_depth={},
        balances=balances or {ex: 1000.0 for ex in exchanges},
        fee_bps=fee_bps_map or {ex: {"spot": 5.0, "perp": 5.0} for ex in exchanges},
        funding_rates=funding_rates or {ex: 0.0001 for ex in exchanges},
        volatility=volatility,
        trend_strength=trend_strength,
        atr=50.0,
        atr_rolling=50.0,
        indicators={"rsi": 50.0, "spread_bps": 5.0, "basis_bps": 2.0, "funding_spread_bps": 1.0},
        timestamp=time.time(),
    )


def _make_intent(
    strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
    symbol="BTCUSDT",
    long_exchange="okx",
    short_exchange="bybit",
    edge_bps=10.0,
    confidence=0.8,
    metadata=None,
) -> TradeIntent:
    return TradeIntent(
        strategy_id=strategy_id,
        symbol=symbol,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        side="cross_exchange_arb",
        confidence=confidence,
        expected_edge_bps=edge_bps,
        stop_loss_bps=15.0,
        metadata=metadata or {},
    )


def _make_plan(strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE, alloc=5000.0) -> AllocationPlan:
    return AllocationPlan(
        strategy_allocations={strategy_id: alloc},
        total_allocatable_capital=alloc,
    )


# ═══════════════════════════════════════════════════════════════
# SystemState Tests
# ═══════════════════════════════════════════════════════════════

class TestSystemState:

    @pytest.mark.asyncio
    async def test_initial_state(self):
        state = SystemState(1000.0, positions_file=":memory:")
        snap = await state.snapshot()
        assert snap["equity"] == 1000.0
        assert snap["open_positions"] == 0
        assert snap["total_exposure"] == 0.0
        assert snap["kill_switch"] is False

    @pytest.mark.asyncio
    async def test_add_remove_position(self):
        state = SystemState(1000.0, positions_file=":memory:")
        pos = OpenPosition(
            position_id="test-1",
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="bybit",
            notional_usd=100.0,
            entry_mid=50000.0,
            stop_loss_bps=10.0,
        )
        await state.add_position(pos)
        snap = await state.snapshot()
        assert snap["open_positions"] == 1
        assert snap["total_exposure"] == 100.0

        removed = await state.remove_position("test-1")
        assert removed is not None
        assert removed.position_id == "test-1"
        snap = await state.snapshot()
        assert snap["open_positions"] == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_position(self):
        state = SystemState(1000.0, positions_file=":memory:")
        removed = await state.remove_position("nonexistent")
        assert removed is None

    @pytest.mark.asyncio
    async def test_equity_tracking(self):
        state = SystemState(1000.0, positions_file=":memory:")
        await state.set_equity(1100.0)
        snap = await state.snapshot()
        assert snap["equity"] == 1100.0
        assert snap["max_equity"] == 1100.0

    @pytest.mark.asyncio
    async def test_apply_realized_pnl(self):
        state = SystemState(1000.0, positions_file=":memory:")
        await state.apply_realized_pnl(50.0)
        snap = await state.snapshot()
        assert snap["equity"] == 1050.0
        assert snap["realized_pnl"] == 50.0

    @pytest.mark.asyncio
    async def test_drawdowns(self):
        state = SystemState(1000.0, positions_file=":memory:")
        await state.set_equity(1000.0)
        await state.set_equity(900.0)
        dd = await state.drawdowns()
        assert dd["portfolio_dd"] == pytest.approx(0.1, abs=0.01)

    @pytest.mark.asyncio
    async def test_kill_switch_permanent(self):
        state = SystemState(1000.0, positions_file=":memory:")
        await state.trigger_kill_switch(permanent=True)
        assert await state.kill_switch_triggered() is True
        # Permanent kill switch does not auto-reset
        state._kill_switch_cooldown_sec = 0.0
        assert await state.kill_switch_triggered() is True

    @pytest.mark.asyncio
    async def test_kill_switch_temporary_auto_reset(self):
        state = SystemState(1000.0, positions_file=":memory:")
        state._kill_switch_cooldown_sec = 0.01
        await state.trigger_kill_switch(permanent=False)
        assert await state.kill_switch_triggered() is True
        await asyncio.sleep(0.02)
        assert await state.kill_switch_triggered() is False

    @pytest.mark.asyncio
    async def test_kill_switch_manual_reset(self):
        state = SystemState(1000.0, positions_file=":memory:")
        await state.trigger_kill_switch(permanent=True)
        await state.reset_kill_switch()
        assert await state.kill_switch_triggered() is False

    @pytest.mark.asyncio
    async def test_strategy_exposure(self):
        state = SystemState(10000.0, positions_file=":memory:")
        pos1 = OpenPosition(
            position_id="p1", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=200.0, entry_mid=50000.0, stop_loss_bps=10.0,
        )
        pos2 = OpenPosition(
            position_id="p2", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="ETHUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=300.0, entry_mid=3000.0, stop_loss_bps=10.0,
        )
        await state.add_position(pos1)
        await state.add_position(pos2)
        assert await state.strategy_exposure(StrategyId.FUTURES_CROSS_EXCHANGE) == 500.0
        assert await state.total_exposure() == 500.0


# ═══════════════════════════════════════════════════════════════
# Position Persistence Tests
# ═══════════════════════════════════════════════════════════════

class TestPositionPersistence:

    def test_serialize_deserialize_roundtrip(self):
        pos = OpenPosition(
            position_id="test-id",
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="bybit",
            notional_usd=500.0,
            entry_mid=50000.0,
            stop_loss_bps=15.0,
            metadata={"entry_long_price": 49990.0, "entry_short_price": 50010.0},
        )
        data = _serialize_position(pos)
        restored = _deserialize_position(data)
        assert restored.position_id == pos.position_id
        assert restored.strategy_id == pos.strategy_id
        assert restored.symbol == pos.symbol
        assert restored.notional_usd == pos.notional_usd
        assert restored.metadata["entry_long_price"] == 49990.0

    @pytest.mark.asyncio
    async def test_persist_and_recover(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmpfile = f.name
            json.dump([], f)
        try:
            state1 = SystemState(1000.0, positions_file=tmpfile)
            pos = OpenPosition(
                position_id="persist-test",
                strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                symbol="ETHUSDT",
                long_exchange="okx",
                short_exchange="htx",
                notional_usd=200.0,
                entry_mid=3000.0,
                stop_loss_bps=10.0,
            )
            await state1.add_position(pos)

            # Create new state instance that reads from same file
            state2 = SystemState(1000.0, positions_file=tmpfile)
            positions = await state2.list_positions()
            assert len(positions) == 1
            assert positions[0].position_id == "persist-test"
            assert positions[0].symbol == "ETHUSDT"
        finally:
            os.unlink(tmpfile)

    @pytest.mark.asyncio
    async def test_persist_removes_on_close(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmpfile = f.name
            json.dump([], f)
        try:
            state = SystemState(1000.0, positions_file=tmpfile)
            pos = OpenPosition(
                position_id="rm-test",
                strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                symbol="BTCUSDT",
                long_exchange="okx",
                short_exchange="bybit",
                notional_usd=100.0,
                entry_mid=50000.0,
                stop_loss_bps=10.0,
            )
            await state.add_position(pos)
            await state.remove_position("rm-test")

            state2 = SystemState(1000.0, positions_file=tmpfile)
            positions = await state2.list_positions()
            assert len(positions) == 0
        finally:
            os.unlink(tmpfile)


# ═══════════════════════════════════════════════════════════════
# RiskEngine Tests
# ═══════════════════════════════════════════════════════════════

class TestRiskEngine:

    @pytest.mark.asyncio
    async def test_approve_valid_intent(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(), state)
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=500.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=50.0,
        )
        assert decision.approved
        assert decision.reason == "approved"

    @pytest.mark.asyncio
    async def test_reject_kill_switch(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        await state.trigger_kill_switch(permanent=True)
        risk = RiskEngine(RiskConfig(), state)
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.reason == "kill_switch_active"

    @pytest.mark.asyncio
    async def test_reject_high_latency(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(api_latency_limit_ms=100), state)
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=200.0,
        )
        assert not decision.approved
        assert decision.reason == "api_latency_limit_exceeded"

    @pytest.mark.asyncio
    async def test_reject_high_slippage(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(max_order_slippage_bps=5.0), state)
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=10.0, leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.reason == "slippage_limit_exceeded"

    @pytest.mark.asyncio
    async def test_reject_max_positions(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(max_open_positions=1), state)
        await state.add_position(OpenPosition(
            position_id="existing", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="ETHUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=100.0, entry_mid=3000.0, stop_loss_bps=10.0,
        ))
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.reason == "max_open_positions_reached"

    @pytest.mark.asyncio
    async def test_reject_stale_orderbook(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(max_orderbook_age_sec=1.0), state)
        snapshot = _make_snapshot()
        # Fake stale orderbook
        old_ob = OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=100000, ask=100010, timestamp=time.time() - 5)
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            orderbooks={"okx": old_ob, "bybit": snapshot.orderbooks["bybit"]},
            spot_orderbooks={},
            orderbook_depth={},
            spot_orderbook_depth={},
            balances={"okx": 1000, "bybit": 1000},
            fee_bps={},
            funding_rates={},
            volatility=0.001,
            trend_strength=0.0,
            atr=50.0,
            atr_rolling=50.0,
            indicators={},
            timestamp=time.time(),
        )
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=50.0,
            snapshot=snapshot,
        )
        assert not decision.approved
        assert decision.reason == "stale_orderbook"

    @pytest.mark.asyncio
    async def test_daily_drawdown_triggers_permanent_kill_switch(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(max_daily_drawdown_pct=0.05), state)
        await state.set_equity(9_400.0)  # 6% drawdown
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.kill_switch_triggered
        # Permanent kill switch should persist
        assert await state.kill_switch_triggered() is True

    @pytest.mark.asyncio
    async def test_latency_breach_streak_triggers_kill_switch(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(
            RiskConfig(api_latency_limit_ms=100, api_latency_breach_limit=3, kill_switch_enabled=True),
            state,
        )
        intent = _make_intent()
        plan = _make_plan()
        for _ in range(2):
            await risk.validate_intent(
                intent, plan, proposed_notional=100.0,
                estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=200.0,
            )
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=200.0,
        )
        assert decision.kill_switch_triggered

    @pytest.mark.asyncio
    async def test_inventory_imbalance_rejection(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        risk = RiskEngine(RiskConfig(max_inventory_imbalance_pct=0.3), state)
        snapshot = _make_snapshot(balances={"okx": 1000.0, "bybit": 100.0})
        intent = _make_intent()
        plan = _make_plan()
        decision = await risk.validate_intent(
            intent, plan, proposed_notional=100.0,
            estimated_slippage_bps=1.0, leverage=1.0, api_latency_ms=50.0,
            snapshot=snapshot,
        )
        assert not decision.approved
        assert decision.reason == "inventory_imbalance"


# ═══════════════════════════════════════════════════════════════
# Execution Engine Tests
# ═══════════════════════════════════════════════════════════════

class TestAtomicExecutionEngine:

    @pytest.mark.asyncio
    async def test_dry_run_creates_position(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        config = ExecutionConfig(dry_run=True)
        venue = SimulatedExecutionVenue()
        monitor = InMemoryMonitoring(logging.getLogger("test"))
        engine = AtomicExecutionEngine(
            config=config,
            venue=venue,
            slippage=SlippageModel(),
            state=state,
            monitor=monitor,
        )
        intent = _make_intent(metadata={"entry_mid": 50000.0, "long_price": 49990.0, "short_price": 50010.0})
        report = await engine.execute_dual_entry(
            intent, notional_usd=100.0, est_book_depth_usd=1_000_000,
            volatility=0.001, latency_ms=50.0,
        )
        assert report.success
        assert report.message == "dry_run_fill"
        positions = await state.list_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_dry_run_multi_leg(self):
        state = SystemState(10_000.0, positions_file=":memory:")
        config = ExecutionConfig(dry_run=True)
        venue = SimulatedExecutionVenue()
        monitor = InMemoryMonitoring(logging.getLogger("test"))
        engine = AtomicExecutionEngine(
            config=config,
            venue=venue,
            slippage=SlippageModel(),
            state=state,
            monitor=monitor,
        )
        intent = _make_intent(metadata={
            "legs": [
                {"exchange": "okx", "symbol": "BTCUSDT", "side": "buy", "quantity_base": 0.001},
                {"exchange": "okx", "symbol": "ETHUSDT", "side": "sell", "quantity_base": 0.01},
                {"exchange": "okx", "symbol": "ETHBTC", "side": "buy", "quantity_base": 0.01},
            ],
        })
        report = await engine.execute_multi_leg_spot(intent)
        assert report.success
        assert report.message == "multi_leg_dry_run"

    @pytest.mark.asyncio
    async def test_determine_leg_order_reliability(self):
        intent = _make_intent(long_exchange="htx", short_exchange="okx")
        first_leg, first_side, second_leg, second_side = AtomicExecutionEngine._determine_leg_order(intent)
        assert first_leg == "okx"
        assert second_leg == "htx"
        assert first_side == "sell"
        assert second_side == "buy"

    @pytest.mark.asyncio
    async def test_determine_exit_leg_order(self):
        pos = OpenPosition(
            position_id="test", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="htx", short_exchange="okx",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=10.0,
        )
        first_ex, first_side, first_size, second_ex, second_side, second_size = (
            AtomicExecutionEngine._determine_exit_leg_order(pos, "sell", "buy", 1.0, 1.0)
        )
        assert first_ex == "okx"
        assert second_ex == "htx"


# ═══════════════════════════════════════════════════════════════
# FuturesCrossExchangeStrategy Tests
# ═══════════════════════════════════════════════════════════════

class TestFuturesCrossExchangeStrategy:

    @pytest.mark.asyncio
    async def test_no_signal_on_tiny_spread(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.1)
        snapshot = _make_snapshot(
            bid_ask_pairs={"okx": (100_000.0, 100_001.0), "bybit": (100_000.5, 100_001.5)},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) == 0

    @pytest.mark.asyncio
    async def test_signal_on_large_spread(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        # okx ask = 99_990, bybit bid = 100_400 -> spread ~0.41%
        # Round-trip fees ~0.21%, so net ~0.20% > 0.01% threshold
        snapshot = _make_snapshot(
            bid_ask_pairs={"okx": (99_980.0, 99_990.0), "bybit": (100_400.0, 100_410.0)},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) > 0
        assert intents[0].long_exchange == "okx"
        assert intents[0].short_exchange == "bybit"

    @pytest.mark.asyncio
    async def test_cooldown_prevents_repeat_signal(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        strategy._signal_cooldown_sec = 10.0
        snapshot = _make_snapshot(
            bid_ask_pairs={"okx": (99_980.0, 99_990.0), "bybit": (100_400.0, 100_410.0)},
        )
        intents1 = await strategy.on_market_snapshot(snapshot)
        intents2 = await strategy.on_market_snapshot(snapshot)
        assert len(intents1) > 0
        # Second call within cooldown should have fewer/no signals for same pair
        price_spread_intents_1 = [i for i in intents1 if i.metadata.get("arb_type") == "price_spread"]
        price_spread_intents_2 = [i for i in intents2 if i.metadata.get("arb_type") == "price_spread"]
        assert len(price_spread_intents_2) < len(price_spread_intents_1)

    @pytest.mark.asyncio
    async def test_needs_two_exchanges(self):
        strategy = FuturesCrossExchangeStrategy()
        snapshot = _make_snapshot(exchanges=["okx"], bid_ask_pairs={"okx": (100_000.0, 100_010.0)})
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) == 0

    @pytest.mark.asyncio
    async def test_funding_rate_arbitrage(self):
        strategy = FuturesCrossExchangeStrategy(funding_threshold_pct=0.005)
        snapshot = _make_snapshot(
            funding_rates={"okx": 0.001, "bybit": -0.0005},  # 0.15% diff
        )
        intents = await strategy.on_market_snapshot(snapshot)
        funding_intents = [i for i in intents if i.metadata.get("arb_type") == "funding_rate"]
        if funding_intents:
            assert funding_intents[0].short_exchange == "okx"
            assert funding_intents[0].long_exchange == "bybit"

    @pytest.mark.asyncio
    async def test_depth_check_blocks_illiquid(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            bid_ask_pairs={"okx": (99_980.0, 99_990.0), "bybit": (100_100.0, 100_110.0)},
        )
        # Add depth data with very low liquidity
        depth = {
            "okx": {"bids": [["100000", "0.001"]], "asks": [["100010", "0.001"]]},
            "bybit": {"bids": [["100005", "0.001"]], "asks": [["100015", "0.001"]]},
        }
        snapshot = MarketSnapshot(
            symbol=snapshot.symbol,
            orderbooks=snapshot.orderbooks,
            spot_orderbooks={},
            orderbook_depth=depth,
            spot_orderbook_depth={},
            balances=snapshot.balances,
            fee_bps=snapshot.fee_bps,
            funding_rates=snapshot.funding_rates,
            volatility=snapshot.volatility,
            trend_strength=snapshot.trend_strength,
            atr=snapshot.atr,
            atr_rolling=snapshot.atr_rolling,
            indicators=snapshot.indicators,
            timestamp=snapshot.timestamp,
        )
        intents = await strategy.on_market_snapshot(snapshot)
        price_intents = [i for i in intents if i.metadata.get("arb_type") == "price_spread"]
        assert len(price_intents) == 0


# ═══════════════════════════════════════════════════════════════
# Capital Allocator Tests
# ═══════════════════════════════════════════════════════════════

class TestCapitalAllocator:

    def test_allocate_basic(self):
        config = RiskConfig(max_strategy_allocation_pct=0.5)
        allocator = CapitalAllocator(config)
        plan = allocator.allocate(
            equity=10_000.0,
            avg_funding_bps=5.0,
            volatility_regime=0.001,
            trend_strength=0.01,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        assert StrategyId.FUTURES_CROSS_EXCHANGE in plan.strategy_allocations
        assert plan.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE] > 0

    def test_allocate_empty_strategies(self):
        allocator = CapitalAllocator(RiskConfig())
        plan = allocator.allocate(equity=10_000.0, avg_funding_bps=0, volatility_regime=0, trend_strength=0, enabled=[])
        assert plan.total_allocatable_capital >= 0


# ═══════════════════════════════════════════════════════════════
# SlippageModel Tests
# ═══════════════════════════════════════════════════════════════

class TestSlippageModel:

    def test_base_slippage(self):
        model = SlippageModel()
        slip = model.estimate(order_notional_usd=100, average_book_depth_usd=1_000_000, volatility=0.0, latency_ms=0.0)
        assert slip >= model.base_bps

    def test_larger_order_more_slippage(self):
        model = SlippageModel()
        small = model.estimate(100, 1_000_000, 0.001, 50.0)
        large = model.estimate(100_000, 1_000_000, 0.001, 50.0)
        assert large > small

    def test_higher_volatility_more_slippage(self):
        model = SlippageModel()
        calm = model.estimate(1000, 1_000_000, 0.0001, 50.0)
        volatile = model.estimate(1000, 1_000_000, 0.05, 50.0)
        assert volatile > calm


# ═══════════════════════════════════════════════════════════════
# Models Tests
# ═══════════════════════════════════════════════════════════════

class TestModels:

    def test_orderbook_snapshot_mid(self):
        ob = OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=100.0, ask=102.0)
        assert ob.mid == 101.0

    def test_strategy_id_enum_values(self):
        assert StrategyId.FUTURES_CROSS_EXCHANGE.value == "futures_cross_exchange"

    def test_open_position_defaults(self):
        pos = OpenPosition(
            position_id="test", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=10.0,
        )
        assert pos.realized_pnl == 0.0
        assert pos.unrealized_pnl == 0.0
        assert pos.opened_at > 0

    def test_execution_report_defaults(self):
        report = ExecutionReport(
            success=True, position_id="x", fill_price_long=100.0,
            fill_price_short=101.0, notional_usd=1000.0, slippage_bps=1.5, message="ok",
        )
        assert report.hedged is False

    def test_risk_decision_defaults(self):
        rd = RiskDecision(approved=True, reason="ok")
        assert rd.kill_switch_triggered is False


# ═══════════════════════════════════════════════════════════════
# Utility Helpers Tests
# ═══════════════════════════════════════════════════════════════

class TestHelpers:

    def test_calculate_spread(self):
        assert calculate_spread(100.0, 99.0) == pytest.approx(1.0101, abs=0.01)
        assert calculate_spread(100.0, 100.0) == 0.0
        assert calculate_spread(0.0, 100.0) == pytest.approx(-100.0)
        assert calculate_spread(100.0, 0.0) == 0.0

    def test_calculate_pnl_long(self):
        pnl = calculate_pnl(entry_price=100.0, exit_price=110.0, size=1.0, side="LONG")
        assert pnl == 10.0

    def test_calculate_pnl_short(self):
        pnl = calculate_pnl(entry_price=100.0, exit_price=90.0, size=1.0, side="SHORT")
        assert pnl == 10.0

    def test_validate_orderbook(self):
        assert validate_orderbook({"bids": [["100", "1"]], "asks": [["101", "1"]]}) is True
        assert validate_orderbook({}) is False
        assert validate_orderbook({"bids": [], "asks": []}) is False
        assert validate_orderbook({"bids": [["100", "1"]], "asks": [["99", "1"]]}) is False

    def test_get_best_bid_ask(self):
        result = get_best_bid_ask({"bids": [["100", "1"]], "asks": [["101", "1"]]})
        assert result == (100.0, 101.0)
        assert get_best_bid_ask({}) is None

    def test_round_down(self):
        assert round_down(1.999, 2) == 1.99
        assert round_down(1.001, 2) == 1.00

    def test_usdt_to_htx(self):
        assert usdt_to_htx("BTCUSDT") == "BTC-USDT"
        assert usdt_to_htx("ETHUSDT") == "ETH-USDT"
        assert usdt_to_htx("BTC-USDT") == "BTC-USDT"
        assert usdt_to_htx("btcusdt") == "BTC-USDT"


# ═══════════════════════════════════════════════════════════════
# Fee Helpers Tests
# ═══════════════════════════════════════════════════════════════

class TestFees:

    def test_fee_bps_from_env(self, monkeypatch):
        monkeypatch.setenv("FEE_BPS_OKX_PERP", "4.5")
        assert fee_bps("okx", "perp") == 4.5

    def test_fee_bps_default_zero(self):
        assert fee_bps("nonexistent_exchange", "futures") == 0.0

    def test_fee_bps_from_snapshot_basic(self):
        snapshot = _make_snapshot(fee_bps_map={"okx": {"spot": 3.0, "perp": 5.0}})
        assert fee_bps_from_snapshot(snapshot, "okx", "perp", "BTCUSDT") == 5.0


# ═══════════════════════════════════════════════════════════════
# Monitoring Tests
# ═══════════════════════════════════════════════════════════════

class TestMonitoring:

    @pytest.mark.asyncio
    async def test_emit_and_metrics(self):
        monitor = InMemoryMonitoring(logging.getLogger("test"))
        await monitor.emit("test_event", {"key": "value"})
        await monitor.emit("test_event", {"key": "value2"})
        text = monitor.metrics_text()
        assert "event_test_event_total 2" in text

    @pytest.mark.asyncio
    async def test_max_events(self):
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"), max_events=5)
        for i in range(10):
            await monitor.emit("evt", {"i": i})
        assert len(monitor.events) == 5


# ═══════════════════════════════════════════════════════════════
# Integration-style: Engine cycle with dry_run
# ═══════════════════════════════════════════════════════════════

class TestEngineIntegration:

    @pytest.mark.asyncio
    async def test_engine_create_and_cycle(self):
        from arbitrage.system.engine import TradingSystemEngine, build_strategies
        from arbitrage.system.providers import SyntheticMarketDataProvider

        config = TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx", "bybit"],
            credentials={},
            starting_equity=10_000.0,
            execution=ExecutionConfig(dry_run=True, cycle_interval_seconds=0.1),
            strategy=StrategyConfig(enabled=["futures_cross_exchange"]),
        )
        provider = SyntheticMarketDataProvider(exchanges=["okx", "bybit"])
        state = SystemState(config.starting_equity, positions_file=":memory:")
        monitor = InMemoryMonitoring(logging.getLogger("test"))
        venue = SimulatedExecutionVenue()
        slippage = SlippageModel()
        execution = AtomicExecutionEngine(
            config=config.execution, venue=venue, slippage=slippage, state=state, monitor=monitor,
        )
        engine = TradingSystemEngine.create(
            config=config, provider=provider, monitor=monitor, execution=execution, state=state,
        )
        # Run one cycle — should not crash
        await engine.run_cycle()
        snap = await state.snapshot()
        assert snap["equity"] == 10_000.0
