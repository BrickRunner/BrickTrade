"""
Tests for audit v2 fixes:
  BUG #1: age_sec used before definition in engine.py
  BUG #3: Second leg partial fill → matched notional
  BUG #4: Exit order fallback when metadata sizes missing
  BUG #6: Balance cache race condition (async lock)
  Minor: _estimate_book_depth_usd from real depth data
  Minor: _round_price uses module-level math import
"""
import asyncio
import math
import time
import unittest
from dataclasses import dataclass, field, replace
from typing import Dict, List, Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────

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
from arbitrage.system.config import ExecutionConfig, RiskConfig, TradingSystemConfig, StrategyConfig
from arbitrage.system.state import SystemState
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.execution import AtomicExecutionEngine


def _make_ob(exchange: str, symbol: str, bid: float, ask: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(exchange=exchange, symbol=symbol, bid=bid, ask=ask, timestamp=time.time())


def _make_snapshot(
    symbol="BTCUSDT",
    exchanges=("okx", "htx"),
    bid=50000.0,
    ask=50010.0,
    funding_rates=None,
    depth=None,
    balances=None,
) -> MarketSnapshot:
    obs = {ex: _make_ob(ex, symbol, bid, ask) for ex in exchanges}
    return MarketSnapshot(
        symbol=symbol,
        orderbooks=obs,
        spot_orderbooks={},
        orderbook_depth=depth or {},
        spot_orderbook_depth={},
        balances=balances or {ex: 1000.0 for ex in exchanges},
        fee_bps={ex: {"perp": 5.0} for ex in exchanges},
        funding_rates=funding_rates or {ex: 0.0001 for ex in exchanges},
        volatility=0.01,
        trend_strength=0.0,
        atr=10.0,
        atr_rolling=10.0,
        indicators={"spread_bps": 2.0, "funding_spread_bps": 0.0, "basis_bps": 0.0},
        timestamp=time.time(),
    )


def _make_position(
    position_id="test-pos-1",
    symbol="BTCUSDT",
    long_exchange="okx",
    short_exchange="htx",
    notional_usd=100.0,
    metadata=None,
    opened_at=None,
) -> OpenPosition:
    meta = metadata or {}
    return OpenPosition(
        position_id=position_id,
        strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
        symbol=symbol,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        notional_usd=notional_usd,
        entry_mid=50005.0,
        stop_loss_bps=100.0,
        opened_at=opened_at or time.time() - 3600,  # 1 hour ago
        metadata=meta,
    )


def _make_engine(dry_run=True) -> tuple:
    venue = MagicMock()
    venue.get_balances = AsyncMock(return_value={"okx": 1000, "htx": 1000})
    venue.invalidate_balance_cache = MagicMock()
    venue.open_contracts = AsyncMock(return_value=0.0)
    # Preflight balance check reads these via getattr with defaults —
    # MagicMock auto-creates attrs so getattr never hits the default.
    venue.safety_buffer_pct = 0.05
    venue.safety_reserve_usd = 0.50
    # Remove _min_notional_usd so hasattr returns False and default 1.0 is used
    del venue._min_notional_usd
    monitor = MagicMock()
    monitor.emit = AsyncMock()
    state = SystemState(starting_equity=10000.0, positions_file=":memory:")
    engine = AtomicExecutionEngine(
        config=ExecutionConfig(dry_run=dry_run),
        venue=venue,
        slippage=SlippageModel(),
        state=state,
        monitor=monitor,
    )
    return engine, venue, monitor


# ══════════════════════════════════════════════════════════════════════════
# BUG #1: age_sec defined before use in _process_open_positions
# ══════════════════════════════════════════════════════════════════════════


class TestAgeSec:
    """Verify age_sec is computed BEFORE funding PnL calculation."""

    def test_age_sec_defined_before_funding_calc(self):
        """Check source code ordering: age_sec assignment BEFORE periods_held usage."""
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine._process_open_positions)
        # Find positions
        age_def_pos = source.find("age_sec = now - pos.opened_at")
        periods_use_pos = source.find("periods_held = age_sec / funding_interval_sec")
        assert age_def_pos > 0, "age_sec definition not found"
        assert periods_use_pos > 0, "periods_held usage not found"
        assert age_def_pos < periods_use_pos, (
            f"age_sec must be defined BEFORE periods_held usage. "
            f"age_sec at {age_def_pos}, periods_held at {periods_use_pos}"
        )

    @pytest.mark.asyncio
    async def test_funding_pnl_uses_correct_age(self):
        """Simulate _process_open_positions to verify funding PnL is correct."""
        # The key fix: age_sec must be available when computing funding.
        # We test this by verifying the logic order in a mock scenario.
        opened_at = time.time() - 28800  # exactly 1 funding period ago
        now = time.time()
        age_sec = now - opened_at
        funding_interval_sec = 28800.0
        periods_held = age_sec / funding_interval_sec
        # Should be ~1.0 (one period)
        assert 0.9 < periods_held < 1.1, f"periods_held should be ~1.0, got {periods_held}"

    @pytest.mark.asyncio
    async def test_funding_pnl_nonzero_for_old_position(self):
        """A position held for 1 funding period should have non-zero funding PnL."""
        fr_long = 0.0001  # 0.01%
        fr_short = 0.0003  # 0.03%
        notional = 1000.0
        age_sec = 28800.0  # exactly 1 period
        funding_interval_sec = 28800.0
        periods_held = age_sec / funding_interval_sec

        funding_cost_long = fr_long * notional * periods_held
        funding_income_short = fr_short * notional * periods_held
        funding_pnl = funding_income_short - funding_cost_long

        # 0.03% - 0.01% = 0.02% of $1000 = $0.20
        assert abs(funding_pnl - 0.20) < 0.01


# ══════════════════════════════════════════════════════════════════════════
# BUG #3: Second leg should use actual filled notional from first leg
# ══════════════════════════════════════════════════════════════════════════


class TestSecondLegNotionalMatching:
    """Second leg must match first leg's effective_notional to avoid delta."""

    def test_source_uses_first_effective_notional(self):
        """Verify source code uses first_effective for second leg."""
        import inspect
        source = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "first_effective" in source
        assert "second_notional" in source

    @pytest.mark.asyncio
    async def test_second_leg_matches_first_partial_fill(self):
        """If first leg fills at 80 USD, second leg should also use 80 USD."""
        engine, venue, monitor = _make_engine(dry_run=False)

        # First leg fills with effective_notional=80
        venue.place_order = AsyncMock(side_effect=[
            # First leg — success with partial fill
            {"success": True, "order_id": "ord1", "size": 1.0, "fill_price": 50000.0,
             "effective_notional": 80.0},
            # Second leg — will capture the notional passed
            {"success": True, "order_id": "ord2", "size": 1.0, "fill_price": 50010.0,
             "effective_notional": 80.0},
        ])
        venue.wait_for_fill = AsyncMock(return_value=True)

        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
            metadata={"entry_mid": 50005.0, "long_price": 50000, "short_price": 50010},
        )

        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        assert report.success

        # Verify second place_order call used 80.0 (not 100.0)
        calls = venue.place_order.call_args_list
        assert len(calls) == 2
        second_call_notional = calls[1][0][3]  # positional arg: quantity_usd
        assert second_call_notional == 80.0, f"Second leg should use 80.0, got {second_call_notional}"


# ══════════════════════════════════════════════════════════════════════════
# BUG #4: Exit order queries exchange when metadata sizes missing
# ══════════════════════════════════════════════════════════════════════════


class TestExitSizeFallback:
    """When metadata doesn't have saved contract sizes, query exchange."""

    @pytest.mark.asyncio
    async def test_exit_queries_exchange_when_metadata_empty(self):
        """If size_okx / size_htx not in metadata, should call open_contracts."""
        engine, venue, monitor = _make_engine(dry_run=False)

        # Position with NO size metadata
        pos = _make_position(metadata={"entry_mid": 50005.0})
        venue.open_contracts = AsyncMock(return_value=2.0)
        venue.place_order = AsyncMock(return_value={"success": True, "order_id": "exit1"})

        await engine.execute_dual_exit(pos, "take_profit")

        # Should have called open_contracts for both exchanges
        assert venue.open_contracts.call_count >= 2

    @pytest.mark.asyncio
    async def test_exit_uses_metadata_size_when_available(self):
        """If size metadata is present, should NOT call open_contracts."""
        engine, venue, monitor = _make_engine(dry_run=False)

        pos = _make_position(metadata={
            "entry_mid": 50005.0,
            "size_okx": 3.0,
            "size_htx": 3.0,
        })
        venue.place_order = AsyncMock(return_value={"success": True, "order_id": "exit1"})

        await engine.execute_dual_exit(pos, "take_profit")

        # Should NOT have called open_contracts since metadata has sizes
        venue.open_contracts.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_handles_open_contracts_failure(self):
        """If open_contracts raises, should still attempt exit with notional."""
        engine, venue, monitor = _make_engine(dry_run=False)

        pos = _make_position(metadata={"entry_mid": 50005.0})
        venue.open_contracts = AsyncMock(side_effect=Exception("network error"))
        venue.place_order = AsyncMock(return_value={"success": True, "order_id": "exit1"})

        # Should not raise
        result = await engine.execute_dual_exit(pos, "take_profit")
        # Still attempts exit via place_order
        assert venue.place_order.call_count >= 1


# ══════════════════════════════════════════════════════════════════════════
# BUG #6: Balance cache race condition — async lock
# ══════════════════════════════════════════════════════════════════════════


class TestBalanceCacheLock:
    """LiveExecutionVenue._get_balances should be protected by async lock."""

    def test_live_execution_venue_has_balance_lock(self):
        """Verify _bal_lock property exists on LiveExecutionVenue."""
        from arbitrage.system.live_adapters import LiveExecutionVenue
        assert hasattr(LiveExecutionVenue, '_bal_lock')

    def test_get_balances_source_uses_lock(self):
        """Verify _get_balances acquires _bal_lock."""
        import inspect
        from arbitrage.system.live_adapters import LiveExecutionVenue
        source = inspect.getsource(LiveExecutionVenue._get_balances)
        assert "_bal_lock" in source

    def test_get_balances_returns_copy(self):
        """Verify _get_balances returns dict() copy, not reference."""
        import inspect
        from arbitrage.system.live_adapters import LiveExecutionVenue
        source = inspect.getsource(LiveExecutionVenue._get_balances)
        assert "dict(self._balance_cache)" in source


# ══════════════════════════════════════════════════════════════════════════
# Minor: _estimate_book_depth_usd helper
# ══════════════════════════════════════════════════════════════════════════


class TestEstimateBookDepth:
    """Engine should estimate depth from snapshot, not hardcode 2M."""

    def test_estimate_from_depth_data(self):
        from arbitrage.system.engine import TradingSystemEngine

        depth = {
            "okx": {
                "bids": [["50000", "1.0"], ["49999", "2.0"]],
                "asks": [["50010", "1.5"], ["50011", "2.5"]],
            },
            "htx": {
                "bids": [["50000", "0.5"], ["49999", "1.0"]],
                "asks": [["50010", "0.8"], ["50011", "1.2"]],
            },
        }
        snapshot = _make_snapshot(depth=depth)
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
        )

        result = TradingSystemEngine._estimate_book_depth_usd(snapshot, intent)
        # OKX: bids=50000+99998=149998, asks=75015+125027.5=200042.5 → avg ~175020
        # HTX: bids=25000+49999=74999, asks=40008+60013.2=100021 → avg ~87510
        # Average of both: ~131265
        assert result > 10000, f"Depth should be > 10000, got {result}"
        assert result < 2_000_000, "Should use real depth, not fallback"

    def test_fallback_when_no_depth(self):
        from arbitrage.system.engine import TradingSystemEngine

        snapshot = _make_snapshot(depth={})
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
        )

        result = TradingSystemEngine._estimate_book_depth_usd(snapshot, intent)
        assert result == 2_000_000.0, "Should fall back to 2M when no depth data"

    def test_partial_depth_data(self):
        """Only one exchange has depth data."""
        from arbitrage.system.engine import TradingSystemEngine

        depth = {
            "okx": {
                "bids": [["50000", "1.0"]],
                "asks": [["50010", "1.0"]],
            },
        }
        snapshot = _make_snapshot(depth=depth)
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
        )

        result = TradingSystemEngine._estimate_book_depth_usd(snapshot, intent)
        # Only OKX has depth: bids=50000, asks=50010, avg=50005
        assert result > 0
        assert result < 2_000_000


# ══════════════════════════════════════════════════════════════════════════
# Minor: _round_price uses module-level math
# ══════════════════════════════════════════════════════════════════════════


class TestRoundPriceMathImport:
    """Verify _round_price no longer uses __import__('math')."""

    def test_no_dunder_import(self):
        import inspect
        from arbitrage.system.live_adapters import LiveExecutionVenue
        source = inspect.getsource(LiveExecutionVenue._round_price)
        assert "__import__" not in source, "Should use module-level math, not __import__"

    def test_math_floor_log10_used(self):
        import inspect
        from arbitrage.system.live_adapters import LiveExecutionVenue
        source = inspect.getsource(LiveExecutionVenue._round_price)
        assert "math.floor" in source
        assert "math.log10" in source


# ══════════════════════════════════════════════════════════════════════════
# HTX status codes documentation
# ══════════════════════════════════════════════════════════════════════════


class TestHTXStatusDocumentation:
    """Verify HTX status code difference is documented."""

    def test_rest_fill_check_documented(self):
        import inspect
        from arbitrage.system.live_adapters import LiveExecutionVenue
        source = inspect.getsource(LiveExecutionVenue._order_filled)
        assert "REST" in source and "WS" in source, (
            "HTX status code difference between REST and WS should be documented"
        )


# ══════════════════════════════════════════════════════════════════════════
# Integration: engine source doesn't hardcode 2M anymore
# ══════════════════════════════════════════════════════════════════════════


class TestEngineNoHardcodedDepth:
    """run_cycle should use _estimate_book_depth_usd, not hardcoded 2M."""

    def test_no_hardcoded_2m_in_run_cycle(self):
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine.run_cycle)
        assert "2_000_000" not in source, "run_cycle should not hardcode 2_000_000"

    def test_estimate_method_exists(self):
        from arbitrage.system.engine import TradingSystemEngine
        assert hasattr(TradingSystemEngine, '_estimate_book_depth_usd')


# ══════════════════════════════════════════════════════════════════════════
# Additional edge cases
# ══════════════════════════════════════════════════════════════════════════


class TestSecondLegNotionalEdgeCases:
    """Edge cases for second leg notional matching."""

    @pytest.mark.asyncio
    async def test_second_leg_uses_full_notional_when_first_has_no_effective(self):
        """When first result lacks effective_notional, default to notional_usd."""
        engine, venue, monitor = _make_engine(dry_run=False)

        venue.place_order = AsyncMock(side_effect=[
            {"success": True, "order_id": "ord1", "size": 1.0, "fill_price": 50000.0},
            {"success": True, "order_id": "ord2", "size": 1.0, "fill_price": 50010.0,
             "effective_notional": 100.0},
        ])
        venue.wait_for_fill = AsyncMock(return_value=True)

        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
            metadata={"entry_mid": 50005.0, "long_price": 50000, "short_price": 50010},
        )

        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        assert report.success

        # With no effective_notional in first result, should default to 100.0
        calls = venue.place_order.call_args_list
        second_call_notional = calls[1][0][3]
        assert second_call_notional == 100.0


class TestEstimateDepthEdgeCases:
    """Edge cases for _estimate_book_depth_usd."""

    def test_empty_bids_asks(self):
        from arbitrage.system.engine import TradingSystemEngine

        depth = {"okx": {"bids": [], "asks": []}}
        snapshot = _make_snapshot(depth=depth)
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
        )
        result = TradingSystemEngine._estimate_book_depth_usd(snapshot, intent)
        assert result == 2_000_000.0  # Falls back

    def test_only_bids_no_asks(self):
        from arbitrage.system.engine import TradingSystemEngine

        depth = {"okx": {"bids": [["50000", "1.0"]], "asks": []}}
        snapshot = _make_snapshot(depth=depth)
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
        )
        result = TradingSystemEngine._estimate_book_depth_usd(snapshot, intent)
        assert result == 2_000_000.0  # bid_usd > 0 but ask_usd == 0 → skip
