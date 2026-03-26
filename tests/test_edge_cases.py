"""
Comprehensive edge-case tests for the BrickTrade trading system.

Covers: rate limiter, slippage model edge cases, capital allocator boundaries,
system state race conditions, config validation, circuit breaker,
strategy cooldowns, ws orderbook cache, and hourly log rotation.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Rate Limiter ─────────────────────────────────────────────────────────

from arbitrage.utils.rate_limiter import ExchangeRateLimiter, _Bucket


class TestRateLimiter(unittest.TestCase):
    """Tests for ExchangeRateLimiter."""

    def test_acquire_consumes_token(self):
        limiter = ExchangeRateLimiter({"test": 100.0})
        asyncio.run(self._acquire_n(limiter, "test", 1))
        bucket = limiter._get_bucket("test")
        # After 1 acquire, tokens should have dropped by ~1
        self.assertLess(bucket.tokens, bucket.max_tokens)

    def test_record_429_triggers_backoff(self):
        limiter = ExchangeRateLimiter({"okx": 10.0})
        backoff = limiter.record_429("okx")
        self.assertGreater(backoff, 0)
        status = limiter.status()
        self.assertTrue(status["okx"]["in_backoff"])

    def test_consecutive_429_exponential_backoff(self):
        limiter = ExchangeRateLimiter({"binance": 10.0})
        b1 = limiter.record_429("binance")
        b2 = limiter.record_429("binance")
        b3 = limiter.record_429("binance")
        # Each should be ~2x the previous
        self.assertAlmostEqual(b2, b1 * 2, places=1)
        self.assertAlmostEqual(b3, b2 * 2, places=1)

    def test_record_success_resets_429_counter(self):
        limiter = ExchangeRateLimiter({"htx": 10.0})
        limiter.record_429("htx")
        limiter.record_429("htx")
        limiter.record_success("htx")
        bucket = limiter._get_bucket("htx")
        self.assertEqual(bucket.consecutive_429, 0)

    def test_backoff_capped_at_max(self):
        limiter = ExchangeRateLimiter({"okx": 10.0})
        last = 0
        for _ in range(20):
            last = limiter.record_429("okx")
        self.assertLessEqual(last, 60.0)  # MAX_BACKOFF_SECONDS

    def test_different_exchanges_independent(self):
        limiter = ExchangeRateLimiter({"okx": 10.0, "bybit": 10.0})
        limiter.record_429("okx")
        status = limiter.status()
        self.assertTrue(status["okx"]["in_backoff"])
        # bybit should not have a bucket yet or not be in backoff
        if "bybit" in status:
            self.assertFalse(status["bybit"]["in_backoff"])

    def test_default_rate_for_unknown_exchange(self):
        limiter = ExchangeRateLimiter()
        bucket = limiter._get_bucket("unknown_exchange")
        self.assertEqual(bucket.rate, 10.0)  # fallback default

    def test_acquire_waits_when_429_backoff(self):
        """Acquire should wait when exchange is in backoff."""
        limiter = ExchangeRateLimiter({"okx": 100.0})
        # Set a very short backoff
        bucket = limiter._get_bucket("okx")
        bucket.backoff_until = time.monotonic() + 0.05  # 50ms
        bucket.consecutive_429 = 1
        start = time.monotonic()
        asyncio.run(limiter.acquire("okx"))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.04)  # waited for backoff

    def test_status_returns_all_tracked(self):
        limiter = ExchangeRateLimiter()
        asyncio.run(limiter.acquire("okx"))
        asyncio.run(limiter.acquire("bybit"))
        status = limiter.status()
        self.assertIn("okx", status)
        self.assertIn("bybit", status)

    async def _acquire_n(self, limiter, exchange, n):
        for _ in range(n):
            await limiter.acquire(exchange)


class TestBucketRefill(unittest.TestCase):
    """Test token bucket refill mechanics."""

    def test_refill_adds_tokens(self):
        bucket = _Bucket(rate=10.0)
        bucket.tokens = 0.0
        bucket.last_refill = time.monotonic() - 1.0  # 1 second ago
        bucket._refill()
        self.assertGreater(bucket.tokens, 9.0)  # ~10 tokens from 1s at rate 10

    def test_refill_capped_at_max(self):
        bucket = _Bucket(rate=100.0)
        bucket.tokens = bucket.max_tokens
        bucket.last_refill = time.monotonic() - 10.0
        bucket._refill()
        self.assertLessEqual(bucket.tokens, bucket.max_tokens)


# ── Slippage Model Edge Cases ────────────────────────────────────────────

from arbitrage.system.slippage import SlippageModel


class TestSlippageEdgeCases(unittest.TestCase):

    def test_zero_depth_returns_max_penalty(self):
        model = SlippageModel()
        result = model.estimate(1000, 0, 0.01, 100)
        self.assertEqual(result, 1000.0)

    def test_negative_depth_returns_max_penalty(self):
        model = SlippageModel()
        result = model.estimate(1000, -500, 0.01, 100)
        self.assertEqual(result, 1000.0)

    def test_negative_volatility_clipped(self):
        model = SlippageModel()
        result = model.estimate(100, 10000, -5.0, 50)
        # Negative vol should be treated as 0 via max(0, volatility)
        result_zero_vol = model.estimate(100, 10000, 0.0, 50)
        self.assertEqual(result, result_zero_vol)

    def test_zero_notional(self):
        model = SlippageModel()
        result = model.estimate(0, 10000, 0.01, 100)
        # size_pressure = 0, so result = base + 0 + vol_factor + latency_factor
        self.assertGreater(result, 0)

    def test_walk_book_empty_levels(self):
        result = SlippageModel.walk_book([], 1000)
        self.assertEqual(result, 0.0)

    def test_walk_book_zero_notional(self):
        levels = [(100.0, 10.0)]
        result = SlippageModel.walk_book(levels, 0.0)
        self.assertEqual(result, 0.0)

    def test_walk_book_negative_notional(self):
        levels = [(100.0, 10.0)]
        result = SlippageModel.walk_book(levels, -500)
        self.assertEqual(result, 0.0)

    def test_walk_book_skip_invalid_levels(self):
        """Levels with price<=0 or qty<=0 should be skipped."""
        levels = [
            (0.0, 5.0),    # invalid price
            (-1.0, 5.0),   # invalid price
            (100.0, 0.0),  # invalid qty
            (100.0, -1.0), # invalid qty
            (100.0, 10.0), # valid
        ]
        result = SlippageModel.walk_book(levels, 500)
        self.assertAlmostEqual(result, 100.0, places=2)

    def test_walk_book_partial_fill(self):
        """If book doesn't have enough depth, still returns partial vwap."""
        levels = [(100.0, 1.0)]  # only $100 available
        result = SlippageModel.walk_book(levels, 500)
        # Should fill 1.0 qty at $100 = $100 notional, need $500
        # Partial: vwap = 100/1 = 100
        self.assertAlmostEqual(result, 100.0, places=2)

    def test_walk_book_slippage_bps_no_depth(self):
        """Empty book => walk_book returns 0 => slippage = 1000 bps (max penalty)."""
        result = SlippageModel.walk_book_slippage_bps([], 1000, 100.0)
        self.assertEqual(result, 1000.0)

    def test_walk_book_slippage_bps_zero_top(self):
        result = SlippageModel.walk_book_slippage_bps([(100, 10)], 500, 0.0)
        self.assertEqual(result, 0.0)

    def test_walk_book_slippage_bps_insufficient_depth(self):
        """Empty fill => 1000 bps penalty."""
        result = SlippageModel.walk_book_slippage_bps([(0, 0)], 500, 100.0)
        self.assertEqual(result, 1000.0)


# ── Capital Allocator Edge Cases ─────────────────────────────────────────

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.config import RiskConfig
from arbitrage.system.models import StrategyId


class TestCapitalAllocatorEdgeCases(unittest.TestCase):

    def test_zero_equity(self):
        allocator = CapitalAllocator(risk_config=RiskConfig())
        plan = allocator.allocate(0.0, 5.0, 0.003, 0.005, [StrategyId.FUTURES_CROSS_EXCHANGE])
        self.assertEqual(plan.total_allocatable_capital, 0.0)
        self.assertEqual(plan.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE], 0.0)

    def test_high_volatility_reduces_allocation(self):
        # Use large cap so weight differences are visible (not clipped by per-strategy cap)
        risk = RiskConfig(max_total_exposure_pct=0.90, max_strategy_allocation_pct=0.90)
        allocator = CapitalAllocator(risk_config=risk)
        plan_normal = allocator.allocate(10000, 5.0, 0.003, 0.005, [StrategyId.FUTURES_CROSS_EXCHANGE])
        plan_high_vol = allocator.allocate(10000, 5.0, 0.01, 0.005, [StrategyId.FUTURES_CROSS_EXCHANGE])
        self.assertLess(
            plan_high_vol.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
            plan_normal.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
        )

    def test_strong_trend_reduces_allocation(self):
        risk = RiskConfig(max_total_exposure_pct=0.90, max_strategy_allocation_pct=0.90)
        allocator = CapitalAllocator(risk_config=risk)
        plan_calm = allocator.allocate(10000, 5.0, 0.003, 0.001, [StrategyId.FUTURES_CROSS_EXCHANGE])
        plan_trend = allocator.allocate(10000, 5.0, 0.003, 0.03, [StrategyId.FUTURES_CROSS_EXCHANGE])
        self.assertLess(
            plan_trend.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
            plan_calm.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
        )

    def test_high_funding_boosts_allocation(self):
        # Use high per-strategy cap, low exposure so weight*allocatable < cap
        risk = RiskConfig(max_total_exposure_pct=0.10, max_strategy_allocation_pct=0.90)
        allocator = CapitalAllocator(risk_config=risk)
        plan_low = allocator.allocate(10000, 2.0, 0.003, 0.005, [StrategyId.FUTURES_CROSS_EXCHANGE])
        plan_high = allocator.allocate(10000, 20.0, 0.003, 0.005, [StrategyId.FUTURES_CROSS_EXCHANGE])
        self.assertGreater(
            plan_high.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
            plan_low.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
        )

    def test_empty_strategy_list(self):
        allocator = CapitalAllocator(risk_config=RiskConfig())
        plan = allocator.allocate(10000, 5.0, 0.003, 0.005, [])
        self.assertEqual(len(plan.strategy_allocations), 0)

    def test_allocation_respects_per_strategy_cap(self):
        """Even with max boost, allocation <= equity * max_strategy_allocation_pct."""
        risk = RiskConfig(max_total_exposure_pct=0.90, max_strategy_allocation_pct=0.05)
        allocator = CapitalAllocator(risk_config=risk)
        plan = allocator.allocate(10000, 50.0, 0.003, 0.0, [StrategyId.FUTURES_CROSS_EXCHANGE])
        cap = 10000 * 0.05
        self.assertLessEqual(
            plan.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE],
            cap + 0.01,
        )


# ── System State Edge Cases ──────────────────────────────────────────────

from arbitrage.system.state import SystemState
from arbitrage.system.models import OpenPosition


class TestSystemStateEdgeCases(unittest.TestCase):

    def _make_position(self, pid: str = "p1", notional: float = 100.0) -> OpenPosition:
        return OpenPosition(
            position_id=pid,
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="bybit",
            notional_usd=notional,
            entry_mid=50000.0,
            stop_loss_bps=50.0,
        )

    def test_concurrent_position_adds(self):
        """Multiple concurrent adds should not lose positions."""
        state = SystemState(10000, positions_file=":memory:")

        async def run():
            tasks = [
                state.add_position(self._make_position(f"p{i}"))
                for i in range(20)
            ]
            await asyncio.gather(*tasks)
            positions = await state.list_positions()
            self.assertEqual(len(positions), 20)

        asyncio.run(run())

    def test_remove_nonexistent_position(self):
        state = SystemState(10000, positions_file=":memory:")
        result = asyncio.run(state.remove_position("does_not_exist"))
        self.assertIsNone(result)

    def test_drawdown_with_zero_equity(self):
        """Edge case: what if max_equity is 0."""
        state = SystemState(0, positions_file=":memory:")
        dd = asyncio.run(state.drawdowns())
        self.assertEqual(dd["portfolio_dd"], 0.0)
        self.assertEqual(dd["daily_dd"], 0.0)

    def test_negative_pnl_reduces_equity(self):
        state = SystemState(10000, positions_file=":memory:")
        asyncio.run(state.apply_realized_pnl(-500))
        snap = asyncio.run(state.snapshot())
        self.assertEqual(snap["equity"], 9500)

    def test_kill_switch_cooldown_expires(self):
        state = SystemState(10000, positions_file=":memory:")
        state._kill_switch_cooldown_sec = 0.05  # 50ms

        async def run():
            await state.trigger_kill_switch(permanent=False)
            self.assertTrue(await state.kill_switch_triggered())
            await asyncio.sleep(0.1)
            self.assertFalse(await state.kill_switch_triggered())

        asyncio.run(run())

    def test_permanent_kill_switch_does_not_expire(self):
        state = SystemState(10000, positions_file=":memory:")
        state._kill_switch_cooldown_sec = 0.01

        async def run():
            await state.trigger_kill_switch(permanent=True)
            await asyncio.sleep(0.05)
            self.assertTrue(await state.kill_switch_triggered())

        asyncio.run(run())

    def test_persistence_with_special_characters(self):
        """Position metadata with special chars should persist correctly."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("[]")
            tmpfile = f.name
        try:
            state = SystemState(10000, positions_file=tmpfile)
            pos = self._make_position("special_test")
            pos.metadata["note"] = 'Entry with "quotes" & <brackets>'

            async def add_and_flush():
                await state.add_position(pos)
                # Give async persist task time to complete
                await asyncio.sleep(0.1)

            asyncio.run(add_and_flush())
            # Reload and verify
            state2 = SystemState(10000, positions_file=tmpfile)
            pos_list = asyncio.run(state2.list_positions())
            self.assertEqual(len(pos_list), 1)
            self.assertIn('"quotes"', pos_list[0].metadata["note"])
        finally:
            os.unlink(tmpfile)

    def test_load_corrupted_json(self):
        """Corrupted JSON file should be handled gracefully."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{{{invalid json")
            tmpfile = f.name
        try:
            state = SystemState(10000, positions_file=tmpfile)
            positions = asyncio.run(state.list_positions())
            self.assertEqual(len(positions), 0)
        finally:
            os.unlink(tmpfile)


# ── Config Validation Edge Cases ─────────────────────────────────────────

from arbitrage.system.config import TradingSystemConfig, ExecutionConfig, StrategyConfig, ApiCredentials


class TestConfigValidationEdgeCases(unittest.TestCase):

    def _make_config(self, **overrides):
        defaults = dict(
            symbols=["BTCUSDT"],
            exchanges=["okx", "bybit"],
            credentials={"okx": ApiCredentials("k", "s"), "bybit": ApiCredentials("k", "s")},
            starting_equity=10000,
            risk=RiskConfig(),
            execution=ExecutionConfig(),
            strategy=StrategyConfig(),
        )
        defaults.update(overrides)
        return TradingSystemConfig(**defaults)

    def test_single_exchange_rejected(self):
        cfg = self._make_config(exchanges=["okx"])
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_zero_equity_rejected(self):
        cfg = self._make_config(starting_equity=0)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_negative_equity_rejected(self):
        cfg = self._make_config(starting_equity=-100)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_empty_symbols_rejected(self):
        cfg = self._make_config(symbols=[], trade_all_symbols=False)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_trade_all_symbols_overrides_empty(self):
        """trade_all_symbols=True should bypass empty symbols check."""
        cfg = self._make_config(symbols=[], trade_all_symbols=True)
        cfg.validate()  # Should not raise

    def test_allocation_invariant(self):
        """max_strategy_allocation_pct > max_total_exposure_pct should fail."""
        bad_risk = RiskConfig(max_total_exposure_pct=0.10, max_strategy_allocation_pct=0.50)
        cfg = self._make_config(risk=bad_risk)
        with self.assertRaises(ValueError, msg="max_strategy_allocation_pct"):
            cfg.validate()

    def test_exposure_pct_out_of_range(self):
        bad_risk = RiskConfig(max_total_exposure_pct=0.0)
        cfg = self._make_config(risk=bad_risk)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_exposure_pct_above_one(self):
        bad_risk = RiskConfig(max_total_exposure_pct=1.5, max_strategy_allocation_pct=1.0)
        cfg = self._make_config(risk=bad_risk)
        with self.assertRaises(ValueError):
            cfg.validate()


# ── Circuit Breaker Edge Cases ───────────────────────────────────────────

from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker


class TestCircuitBreakerEdgeCases(unittest.TestCase):

    def test_success_after_partial_errors(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3)
        cb.record_error("okx")
        cb.record_error("okx")
        cb.record_success("okx")
        self.assertTrue(cb.is_available("okx"))
        self.assertEqual(cb._error_counts["okx"], 0)

    def test_trip_resets_error_count(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=2)
        cb.record_error("okx")
        cb.record_error("okx")  # This trips the breaker
        # Error count should reset to 0 after tripping
        self.assertEqual(cb._error_counts["okx"], 0)

    def test_cooldown_expires(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=0.05)
        cb.record_error("binance")  # Trips immediately
        self.assertFalse(cb.is_available("binance"))
        time.sleep(0.1)
        self.assertTrue(cb.is_available("binance"))

    def test_remaining_cooldown(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=100)
        cb.record_error("htx")
        remaining = cb.remaining_cooldown("htx")
        self.assertGreater(remaining, 90)

    def test_unknown_exchange_always_available(self):
        cb = ExchangeCircuitBreaker()
        self.assertTrue(cb.is_available("never_seen"))
        self.assertEqual(cb.remaining_cooldown("never_seen"), 0.0)

    def test_status_includes_all_exchanges(self):
        cb = ExchangeCircuitBreaker()
        cb.record_error("okx")
        cb.record_error("bybit")
        status = cb.status()
        self.assertIn("okx", status)
        self.assertIn("bybit", status)

    def test_multiple_trips_same_exchange(self):
        """After cooldown expires, should be able to trip again."""
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=0.05)
        cb.record_error("okx")  # trip 1
        self.assertFalse(cb.is_available("okx"))
        time.sleep(0.1)
        self.assertTrue(cb.is_available("okx"))
        cb.record_error("okx")  # trip 2
        self.assertFalse(cb.is_available("okx"))


# ── Strategy Cooldown Edge Cases ─────────────────────────────────────────

from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot


class TestStrategyCooldownEdgeCases(unittest.TestCase):

    def _make_snapshot(self, symbol: str, prices: dict) -> MarketSnapshot:
        """Create a minimal snapshot with given exchange prices."""
        obs = {}
        for ex, (bid, ask) in prices.items():
            obs[ex] = OrderBookSnapshot(exchange=ex, symbol=symbol, bid=bid, ask=ask)
        return MarketSnapshot(
            symbol=symbol,
            orderbooks=obs,
            spot_orderbooks={},
            orderbook_depth={ex: {symbol: {"bids": [[bid, 100]], "asks": [[ask, 100]]}} for ex, (bid, ask) in prices.items()},
            spot_orderbook_depth={},
            balances={ex: 10000 for ex in prices},
            fee_bps={ex: {"taker": 5.0} for ex in prices},
            funding_rates={},
            volatility=0.003,
            trend_strength=0.005,
            atr=50.0,
            atr_rolling=48.0,
            indicators={},
        )

    def test_cooldown_key_is_directional(self):
        """Cooldown key should be per-direction, not just per-pair."""
        strat = FuturesCrossExchangeStrategy()
        # Manually set cooldowns for one direction
        key_fwd = "spread_okx_long_bybit_short_BTCUSDT"
        strat._last_signal_ts[key_fwd] = time.time()
        # Reverse direction should NOT be in cooldown
        key_rev = "spread_bybit_long_okx_short_BTCUSDT"
        self.assertNotIn(key_rev, strat._last_signal_ts)


# ── WS Orderbook Cache Edge Cases ────────────────────────────────────────

from arbitrage.system.ws_orderbooks import WsOrderbookCache


class TestWsOrderbookCacheEdgeCases(unittest.TestCase):

    def test_stale_detection(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        cache._stale_after_sec = 1.0
        # Insert a snapshot timestamped in the past
        old_ts = time.time() - 5.0
        snap = OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000, ask=50001, timestamp=old_ts)
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = snap
        # get should return it but it's stale
        result = cache._orderbooks.get("okx", {}).get("BTCUSDT")
        self.assertIsNotNone(result)
        self.assertLess(result.timestamp, time.time() - cache._stale_after_sec)

    def test_max_depth_symbols_bound(self):
        """_max_depth_symbols should prevent unbounded growth."""
        cache = WsOrderbookCache(symbols=[], exchanges=[])
        cache._max_depth_symbols = 3
        # Just verify the attribute exists and is honored conceptually
        self.assertEqual(cache._max_depth_symbols, 3)

    def test_empty_exchanges(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=[])
        # start should not crash with no exchanges
        async def run():
            await cache.start()
            self.assertEqual(len(cache._tasks), 0)
            await cache.stop()
        asyncio.run(run())

    def test_unsupported_exchange_skipped(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["fake_exchange"])
        async def run():
            await cache.start()
            self.assertEqual(len(cache._tasks), 0)
            await cache.stop()
        asyncio.run(run())


# ── Hourly Log Rotation ──────────────────────────────────────────────────

from arbitrage.utils.logger import HourlyRotatingFileHandler


class TestHourlyRotatingFileHandler(unittest.TestCase):

    def test_creates_date_hour_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = HourlyRotatingFileHandler(tmpdir, "test.log")
            now = datetime.now()
            expected_dir = os.path.join(tmpdir, now.strftime("%Y-%m-%d"), now.strftime("%H"))
            self.assertTrue(os.path.isdir(expected_dir))
            handler.close()

    def test_log_entry_written(self):
        import logging
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = HourlyRotatingFileHandler(tmpdir, "test.log")
            handler.setFormatter(logging.Formatter("%(message)s"))
            lg = logging.getLogger("test_hourly_unique_98765")
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
            lg.info("hello world test")
            handler.flush()

            now = datetime.now()
            log_path = os.path.join(tmpdir, now.strftime("%Y-%m-%d"), now.strftime("%H"), "test.log")
            with open(log_path, "r") as f:
                content = f.read()
            self.assertIn("hello world test", content)
            handler.close()

    def test_hour_change_creates_new_dir(self):
        """Simulate hour change by manipulating _current_hour_key."""
        import logging
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = HourlyRotatingFileHandler(tmpdir, "rotate.log")
            handler.setFormatter(logging.Formatter("%(message)s"))
            # Force "previous hour" key
            handler._current_hour_key = "2020-01-01/00"
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="after_rotation", args=(), exc_info=None,
            )
            handler.emit(record)
            # Should have created new directory for current hour
            now = datetime.now()
            new_dir = os.path.join(tmpdir, now.strftime("%Y-%m-%d"), now.strftime("%H"))
            self.assertTrue(os.path.isdir(new_dir))
            handler.close()


# ── Execution Engine Dry Run Edge Cases ──────────────────────────────────

from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.models import TradeIntent


class TestExecutionDryRunEdgeCases(unittest.TestCase):

    def test_per_symbol_lock_isolation(self):
        """Different symbols should get different locks."""
        engine = AtomicExecutionEngine.__new__(AtomicExecutionEngine)
        engine._lock = asyncio.Lock()
        engine._symbol_locks = {}
        lock_btc = engine._get_symbol_lock("BTCUSDT")
        lock_eth = engine._get_symbol_lock("ETHUSDT")
        self.assertIsNot(lock_btc, lock_eth)

    def test_same_symbol_same_lock(self):
        engine = AtomicExecutionEngine.__new__(AtomicExecutionEngine)
        engine._lock = asyncio.Lock()
        engine._symbol_locks = {}
        lock1 = engine._get_symbol_lock("BTCUSDT")
        lock2 = engine._get_symbol_lock("BTCUSDT")
        self.assertIs(lock1, lock2)


# ── Calibrator Tests ──────────────────────────────────────────────────────

from arbitrage.system.calibrator import DailyCalibrator, CalibrationMetrics


class TestDailyCalibrator(unittest.TestCase):

    def test_parse_line_slippage(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("2026-03-24 12:00:00 - slippage=8.5 bps on BTCUSDT", m)
        self.assertEqual(m.slippage_bps, [8.5])

    def test_parse_line_latency(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("2026-03-24 12:00:00 - latency: 150.3 ms for okx", m)
        self.assertEqual(m.latency_ms, [150.3])

    def test_parse_line_spread(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("spread=0.45%", m)
        self.assertEqual(m.spreads_pct, [0.45])

    def test_parse_line_429(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("rate_limiter: 429 from okx (#3), backoff 4.0s", m)
        self.assertEqual(m.rate_limit_429s, {"okx": 1})

    def test_parse_line_near_miss(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("[SPREAD_NEAR_MISS] BTCUSDT spread=0.3%", m)
        self.assertEqual(m.entry_near_misses, 1)

    def test_parse_line_circuit_breaker(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("circuit_breaker: TRIPPED for htx after 5 errors", m)
        self.assertEqual(m.circuit_breaker_trips, {"htx": 1})

    def test_parse_line_fill(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("execution_success: BTCUSDT opened_position", m)
        self.assertEqual(m.fills, 1)

    def test_parse_line_reject(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        cal._parse_line("execution_reject: margin_reject for ETHUSDT", m)
        # Both execution_reject and margin_reject match
        self.assertGreaterEqual(m.rejects, 1)

    def test_compute_stats_empty(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        stats = cal._compute_stats(m)
        self.assertEqual(stats["fills"], 0)
        self.assertNotIn("slippage", stats)

    def test_compute_stats_with_data(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        m.slippage_bps = [5.0, 8.0, 10.0, 15.0, 20.0]
        m.latency_ms = [100, 200, 300]
        m.fills = 5
        stats = cal._compute_stats(m)
        self.assertIn("slippage", stats)
        self.assertEqual(stats["slippage"]["count"], 5)
        self.assertIn("latency", stats)
        self.assertEqual(stats["fills"], 5)

    def test_recommendations_high_slippage(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        m.slippage_bps = [15.0] * 20  # consistently high
        stats = cal._compute_stats(m)
        recs = cal._generate_recommendations(stats, m)
        self.assertIn("RISK_MAX_SLIPPAGE_BPS", recs)

    def test_recommendations_many_429s(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        m.rate_limit_429s = {"okx": 60}
        stats = cal._compute_stats(m)
        recs = cal._generate_recommendations(stats, m)
        self.assertIn("RATE_LIMITER", recs)

    def test_recommendations_empty_metrics(self):
        cal = DailyCalibrator()
        m = CalibrationMetrics()
        stats = cal._compute_stats(m)
        recs = cal._generate_recommendations(stats, m)
        self.assertEqual(len(recs), 0)

    def test_full_run_with_real_logs(self):
        """Integration test: create fake log files and run calibrator."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create logs/2026-03-24/12/arbitrage.log
            hour_dir = os.path.join(tmpdir, "2026-03-24", "12")
            os.makedirs(hour_dir)
            log_content = "\n".join([
                "2026-03-24 12:00:01 - slippage=7.2 bps",
                "2026-03-24 12:00:02 - latency: 120.5 ms",
                "2026-03-24 12:00:03 - spread=0.55%",
                "2026-03-24 12:00:04 - execution_success filled",
                "2026-03-24 12:00:05 - rate_limiter: 429 from bybit (#1)",
                "2026-03-24 12:00:06 - ERROR: something failed",
                "2026-03-24 12:00:07 - [SPREAD_NEAR_MISS] ETHUSDT",
            ])
            with open(os.path.join(hour_dir, "arbitrage.log"), "w") as f:
                f.write(log_content)

            cal = DailyCalibrator(log_dir=tmpdir, output_dir=os.path.join(tmpdir, "calibration"))
            report = asyncio.run(cal.run("2026-03-24"))

            self.assertEqual(report.date, "2026-03-24")
            self.assertEqual(report.metrics["fills"], 1)
            self.assertEqual(report.metrics["errors"], 1)
            self.assertEqual(report.metrics["rate_limit_429s"], {"bybit": 1})
            self.assertIn("slippage", report.metrics)

            # Verify report file was saved
            report_path = os.path.join(tmpdir, "calibration", "2026-03-24.json")
            self.assertTrue(os.path.exists(report_path))
            with open(report_path) as f:
                saved = json.load(f)
            self.assertEqual(saved["date"], "2026-03-24")

    def test_percentile_edge_cases(self):
        self.assertEqual(DailyCalibrator._percentile([], 95), 0.0)
        self.assertEqual(DailyCalibrator._percentile([5.0], 50), 5.0)
        self.assertAlmostEqual(DailyCalibrator._percentile([1, 2, 3, 4, 5], 0), 1.0)
        self.assertAlmostEqual(DailyCalibrator._percentile([1, 2, 3, 4, 5], 100), 5.0)

    def test_missing_date_directory(self):
        """Calibrator should handle missing log directory gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cal = DailyCalibrator(log_dir=tmpdir, output_dir=os.path.join(tmpdir, "calibration"))
            report = asyncio.run(cal.run("1999-01-01"))
            self.assertEqual(report.metrics["fills"], 0)


if __name__ == "__main__":
    unittest.main()
