"""Tests for code review fixes."""
from __future__ import annotations

import asyncio
import ast
import inspect
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbitrage.system.config import ExecutionConfig, RiskConfig, TradingSystemConfig
from arbitrage.system.models import (
    AllocationPlan, ExecutionReport, MarketSnapshot, OpenPosition,
    OrderBookSnapshot, RiskDecision, StrategyId, TradeIntent,
)
from arbitrage.system.risk import RiskEngine
from arbitrage.system.state import SystemState
from arbitrage.system.strategies.triangular_arbitrage import TriangularArbitrageStrategy


def _snap(**kw):
    d = dict(symbol="BTCUSDT", orderbooks={}, spot_orderbooks={},
             orderbook_depth={}, spot_orderbook_depth={}, balances={},
             fee_bps={}, funding_rates={}, volatility=0.02,
             trend_strength=0.0, atr=0.0, atr_rolling=0.0, indicators={})
    d.update(kw)
    return MarketSnapshot(**d)


def _intent(**kw):
    d = dict(strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE, symbol="BTCUSDT",
             long_exchange="bybit", short_exchange="okx", side="long",
             confidence=0.8, expected_edge_bps=5.0, stop_loss_bps=10.0,
             notional_usd=100.0, metadata={})
    d.update(kw)
    return TradeIntent(**d)


def _alloc(**kw):
    d = dict(strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
             total_allocatable_capital=10000.0)
    d.update(kw)
    return AllocationPlan(**d)


# ===== FIX #1: engine.py unverified hedge -> temporary kill switch =====

class TestFix1KillSwitchTemporary:
    def test_source_uses_permanent_false(self):
        import arbitrage.system.engine as eng
        src = inspect.getsource(eng)
        assert "trigger_kill_switch(permanent=False)" in src

    def test_engine_never_uses_permanent_true(self):
        import arbitrage.system.engine as eng
        src = inspect.getsource(eng)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "trigger_kill_switch":
                    for kw in node.keywords:
                        if kw.arg == "permanent" and isinstance(kw.value, ast.Constant):
                            assert kw.value.value is not True

    @pytest.mark.asyncio
    async def test_temporary_expires(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        state._kill_switch_cooldown_sec = 0.05
        state._kill_switch_grace_sec = 0.0
        await state.trigger_kill_switch(permanent=False)
        assert await state.kill_switch_triggered() is True
        await asyncio.sleep(0.06)
        assert await state.kill_switch_triggered() is False

    @pytest.mark.asyncio
    async def test_permanent_does_not_expire(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        state._kill_switch_cooldown_sec = 0.05
        await state.trigger_kill_switch(permanent=True)
        await asyncio.sleep(0.07)
        assert await state.kill_switch_triggered() is True

    @pytest.mark.asyncio
    async def test_reset_clears_permanent(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        await state.trigger_kill_switch(permanent=True)
        assert await state.kill_switch_triggered() is True
        await state.reset_kill_switch()
        assert await state.kill_switch_triggered() is False


# ===== FIX #2: risk.py single snapshot() call =====

class TestFix2SingleSnapshot:
    def test_validate_intent_calls_snapshot_once(self):
        src = inspect.getsource(RiskEngine.validate_intent)
        count = src.count("await self.state.snapshot()")
        assert count == 1, f"Expected 1 snapshot() call, found {count}"

    def test_reuse_comment_present(self):
        src = inspect.getsource(RiskEngine.validate_intent)
        assert "Reuse" in src or "reuse" in src or "same snapshot" in src.lower()


# ===== FIX #3: per-symbol position limit =====

class TestFix3PerSymbolLimit:
    @pytest.mark.asyncio
    async def test_symbol_position_count(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        pos1 = OpenPosition(position_id="p1", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="bybit", short_exchange="okx",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=10.0)
        pos2 = OpenPosition(position_id="p2", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=10.0)
        pos3 = OpenPosition(position_id="p3", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="ETHUSDT", long_exchange="bybit", short_exchange="okx",
            notional_usd=100.0, entry_mid=3000.0, stop_loss_bps=10.0)
        await state.add_position(pos1)
        await state.add_position(pos2)
        await state.add_position(pos3)
        assert await state.symbol_position_count("BTCUSDT") == 2
        assert await state.symbol_position_count("ETHUSDT") == 1
        assert await state.symbol_position_count("SOLUSDT") == 0

    @pytest.mark.asyncio
    async def test_risk_rejects_over_symbol_limit(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk_cfg = RiskConfig(max_positions_per_symbol=2, max_open_positions=20)
        risk = RiskEngine(config=risk_cfg, state=state)
        for i in range(2):
            pos = OpenPosition(position_id=f"p{i}", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                symbol="BTCUSDT", long_exchange="bybit", short_exchange="okx",
                notional_usd=50.0, entry_mid=50000.0, stop_loss_bps=10.0)
            await state.add_position(pos)
        intent = _intent(symbol="BTCUSDT")
        alloc = _alloc()
        decision = await risk.validate_intent(intent, alloc, 50.0, 1.0, 1.0, 100.0)
        assert not decision.approved
        assert decision.reason == "max_positions_per_symbol"

    @pytest.mark.asyncio
    async def test_risk_allows_different_symbol(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk_cfg = RiskConfig(max_positions_per_symbol=1, max_open_positions=20)
        risk = RiskEngine(config=risk_cfg, state=state)
        pos = OpenPosition(position_id="p1", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="bybit", short_exchange="okx",
            notional_usd=50.0, entry_mid=50000.0, stop_loss_bps=10.0)
        await state.add_position(pos)
        intent = _intent(symbol="ETHUSDT")
        alloc = _alloc()
        decision = await risk.validate_intent(intent, alloc, 50.0, 1.0, 1.0, 100.0)
        assert decision.approved

    def test_config_has_max_positions_per_symbol(self):
        cfg = RiskConfig()
        assert hasattr(cfg, "max_positions_per_symbol")
        assert cfg.max_positions_per_symbol == 2

    def test_config_from_env_reads_per_symbol(self):
        with patch.dict(os.environ, {"RISK_MAX_POSITIONS_PER_SYMBOL": "5"}, clear=False):
            cfg = TradingSystemConfig.from_env()
            assert cfg.risk.max_positions_per_symbol == 5


# ===== FIX #4: execution.py first leg timeout =====

class TestFix4FirstLegTimeout:
    def test_wait_for_in_first_leg(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "asyncio.wait_for" in src, "First leg must use asyncio.wait_for"
        assert "first_leg_timeout_sec" in src

    def test_timeout_is_2x_order_timeout(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "order_timeout_ms / 1000 * 2" in src

    @pytest.mark.asyncio
    async def test_first_leg_timeout_returns_order_timeout(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        from arbitrage.system.slippage import SlippageModel
        cfg = ExecutionConfig(order_timeout_ms=100, dry_run=False)
        venue = MagicMock()
        async def slow_order(*a, **kw):
            await asyncio.sleep(10)
        venue.place_order = slow_order
        venue.invalidate_balance_cache = MagicMock()
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = AsyncMock()
        slippage = SlippageModel()
        engine = AtomicExecutionEngine(config=cfg, venue=venue, slippage=slippage,
                                       state=state, monitor=monitor)
        intent = _intent()
        report = await engine.execute_dual_entry(intent, 100.0, 50000.0, 0.02, 50.0)
        assert not report.success
        assert report.message == "order_timeout"


# ===== FIX #5: hedge uses effective notional =====

class TestFix5EffectiveNotional:
    def test_first_effective_in_source(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "first_effective" in src
        assert "effective_notional" in src

    def test_second_notional_uses_first_effective(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "second_notional = min(notional_usd, first_effective)" in src

    def test_hedge_uses_first_effective(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        idx_hedge = src.find("_hedge_first_leg(")
        assert idx_hedge > 0
        hedge_call = src[idx_hedge:idx_hedge+200]
        assert "first_effective" in hedge_call


# ===== FIX #6: triangular uses exchange fees from snapshot =====

class TestFix6TriangularFees:
    def test_total_fee_bps_accepts_snapshot(self):
        strat = TriangularArbitrageStrategy()
        sig = inspect.signature(strat._total_fee_bps)
        params = list(sig.parameters.keys())
        assert "snapshot" in params
        assert "exchange" in params

    def test_default_fees_without_snapshot(self):
        # use_maker_legs defaults to 2: 2*maker + 1*taker
        strat = TriangularArbitrageStrategy(fee_per_leg_pct=0.10, maker_fee_per_leg_pct=0.02)
        bps = strat._total_fee_bps()
        # 2 * 0.02 * 100 + 1 * 0.10 * 100 = 4 + 10 = 14 bps
        expected = 14.0
        assert abs(bps - expected) < 0.01

    def test_default_fees_all_taker(self):
        # Explicitly 0 maker legs: pure taker
        strat = TriangularArbitrageStrategy(fee_per_leg_pct=0.10, use_maker_legs=0)
        bps = strat._total_fee_bps()
        expected = 3 * 0.10 * 100  # 30 bps
        assert abs(bps - expected) < 0.01

    def test_real_fees_from_snapshot(self):
        # use_maker_legs=2 default: 2*10*0.4 + 1*10 = 8+10 = 18
        strat = TriangularArbitrageStrategy()
        snap = _snap(fee_bps={"bybit": {"spot": 10.0}})
        bps = strat._total_fee_bps(snap, "bybit")
        assert abs(bps - 18.0) < 0.01

    def test_real_fees_all_taker(self):
        strat = TriangularArbitrageStrategy(use_maker_legs=0)
        snap = _snap(fee_bps={"bybit": {"spot": 10.0}})
        bps = strat._total_fee_bps(snap, "bybit")
        # 0 maker + 3 taker * 10 = 30 bps
        assert abs(bps - 30.0) < 0.01

    def test_real_fees_with_maker_legs(self):
        strat = TriangularArbitrageStrategy(use_maker_legs=2)
        snap = _snap(fee_bps={"bybit": {"spot": 10.0}})
        bps = strat._total_fee_bps(snap, "bybit")
        # 2 maker legs at 10*0.4=4 bps + 1 taker at 10 bps = 18 bps
        assert abs(bps - 18.0) < 0.01

    def test_falls_back_to_perp_fees(self):
        # use_maker_legs=2 default: 2*8*0.4 + 1*8 = 6.4+8 = 14.4
        strat = TriangularArbitrageStrategy()
        snap = _snap(fee_bps={"bybit": {"perp": 8.0}})
        bps = strat._total_fee_bps(snap, "bybit")
        assert abs(bps - 14.4) < 0.01

    def test_falls_back_to_defaults_if_no_exchange_data(self):
        # No data for bybit -> use configured defaults (use_maker_legs=2)
        strat = TriangularArbitrageStrategy(fee_per_leg_pct=0.10, maker_fee_per_leg_pct=0.02)
        snap = _snap(fee_bps={"okx": {"spot": 10.0}})
        bps = strat._total_fee_bps(snap, "bybit")  # no bybit data
        expected = 14.0  # 2*0.02*100 + 1*0.10*100
        assert abs(bps - expected) < 0.01

    def test_calc_profit_uses_real_fees(self):
        src = inspect.getsource(TriangularArbitrageStrategy._calc_profit)
        assert "use_real_fees" in src

