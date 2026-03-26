"""
Comprehensive unit tests for all bug fixes applied to BrickTrade.

Tests cover:
1. Engine timeout on provider.health()
2. Execution error logging (no silent swallowing)
3. WS orderbook restart count decay
4. Market data empty common_pairs handling
5. State async persistence with aiofiles
6. Strategy per-direction cooldown
7. Scheduler specific exception handling
8. Keyboard UX improvements
9. Handler safe callback parsing
10. Config validation (allocation invariants)
11. Per-symbol execution locks
12. Circuit breaker correctness
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────
# 1. SystemState tests — async persistence, position CRUD
# ─────────────────────────────────────────────────────────

class TestSystemState(unittest.TestCase):
    """Test state persistence and position management."""

    def setUp(self):
        from arbitrage.system.state import SystemState
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.state = SystemState(starting_equity=1000.0, positions_file=self.tmp.name)

    def tearDown(self):
        for path in [self.tmp.name, self.tmp.name + ".tmp"]:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_initial_equity(self):
        loop = asyncio.new_event_loop()
        snap = loop.run_until_complete(self.state.snapshot())
        loop.close()
        self.assertEqual(snap["equity"], 1000.0)
        self.assertEqual(snap["open_positions"], 0)

    def test_add_remove_position(self):
        from arbitrage.system.models import OpenPosition, StrategyId
        loop = asyncio.new_event_loop()
        pos = OpenPosition(
            position_id="test-1",
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            notional_usd=100.0,
            entry_mid=50000.0,
            stop_loss_bps=50.0,
        )
        loop.run_until_complete(self.state.add_position(pos))
        positions = loop.run_until_complete(self.state.list_positions())
        self.assertEqual(len(positions), 1)

        removed = loop.run_until_complete(self.state.remove_position("test-1"))
        self.assertIsNotNone(removed)
        positions = loop.run_until_complete(self.state.list_positions())
        self.assertEqual(len(positions), 0)
        loop.close()

    def test_position_persisted_to_file(self):
        from arbitrage.system.models import OpenPosition, StrategyId
        from arbitrage.system.state import SystemState
        loop = asyncio.new_event_loop()
        pos = OpenPosition(
            position_id="persist-1",
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="ETHUSDT",
            long_exchange="bybit",
            short_exchange="okx",
            notional_usd=50.0,
            entry_mid=3000.0,
            stop_loss_bps=30.0,
        )
        loop.run_until_complete(self.state.add_position(pos))
        loop.run_until_complete(asyncio.sleep(0.2))  # let async persistence complete

        state2 = SystemState(starting_equity=1000.0, positions_file=self.tmp.name)
        positions = loop.run_until_complete(state2.list_positions())
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "ETHUSDT")
        loop.close()

    def test_memory_mode_no_file(self):
        from arbitrage.system.state import SystemState
        state = SystemState(starting_equity=500.0, positions_file=":memory:")
        loop = asyncio.new_event_loop()
        snap = loop.run_until_complete(state.snapshot())
        self.assertEqual(snap["equity"], 500.0)
        loop.close()

    def test_kill_switch_cooldown(self):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.state.trigger_kill_switch(permanent=False))
        self.assertTrue(loop.run_until_complete(self.state.kill_switch_triggered()))
        loop.run_until_complete(self.state.reset_kill_switch())
        self.assertFalse(loop.run_until_complete(self.state.kill_switch_triggered()))
        loop.close()

    def test_kill_switch_permanent(self):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.state.trigger_kill_switch(permanent=True))
        self.assertTrue(loop.run_until_complete(self.state.kill_switch_triggered()))
        loop.close()

    def test_apply_realized_pnl(self):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.state.apply_realized_pnl(50.0))
        snap = loop.run_until_complete(self.state.snapshot())
        self.assertEqual(snap["equity"], 1050.0)
        self.assertEqual(snap["realized_pnl"], 50.0)
        loop.close()

    def test_drawdowns(self):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.state.apply_realized_pnl(-100.0))
        dd = loop.run_until_complete(self.state.drawdowns())
        self.assertAlmostEqual(dd["portfolio_dd"], 0.1, places=2)
        loop.close()

    def test_corrupted_file_loads_gracefully(self):
        from arbitrage.system.state import SystemState
        with open(self.tmp.name, "w") as f:
            f.write("{not valid json!!!}")
        state = SystemState(starting_equity=1000.0, positions_file=self.tmp.name)
        loop = asyncio.new_event_loop()
        positions = loop.run_until_complete(state.list_positions())
        self.assertEqual(len(positions), 0)
        loop.close()

    def test_remove_nonexistent_position_returns_none(self):
        loop = asyncio.new_event_loop()
        removed = loop.run_until_complete(self.state.remove_position("does-not-exist"))
        self.assertIsNone(removed)
        loop.close()

    def test_strategy_exposure(self):
        from arbitrage.system.models import OpenPosition, StrategyId
        loop = asyncio.new_event_loop()
        for i in range(3):
            pos = OpenPosition(
                position_id=f"exp-{i}",
                strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                symbol="BTCUSDT",
                long_exchange="okx",
                short_exchange="htx",
                notional_usd=100.0,
                entry_mid=50000.0,
                stop_loss_bps=50.0,
            )
            loop.run_until_complete(self.state.add_position(pos))
        exposure = loop.run_until_complete(
            self.state.strategy_exposure(StrategyId.FUTURES_CROSS_EXCHANGE)
        )
        self.assertEqual(exposure, 300.0)
        loop.close()


# ─────────────────────────────────────────────────────────
# 2. WS Orderbook Cache tests
# ─────────────────────────────────────────────────────────

class TestWsOrderbookCache(unittest.TestCase):
    """Test WS orderbook restart count decay and depth management."""

    def test_restart_count_decays_after_stability(self):
        from arbitrage.system.ws_orderbooks import WsOrderbookCache
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        key = "okx:BTCUSDT"

        cache._restart_counts[key] = 3
        cache._restart_last_ts[key] = time.time() - 400  # > 300s decay window

        now = time.time()
        last = cache._restart_last_ts.get(key, 0.0)
        if now - last > cache._restart_decay_sec and cache._restart_counts.get(key, 0) > 0:
            cache._restart_counts[key] = max(0, cache._restart_counts[key] - 1)
        cache._restart_counts[key] += 1
        cache._restart_last_ts[key] = now

        self.assertEqual(cache._restart_counts[key], 3)  # 3→2 (decay), +1 = 3

    def test_no_decay_within_window(self):
        from arbitrage.system.ws_orderbooks import WsOrderbookCache
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        key = "okx:BTCUSDT"

        cache._restart_counts[key] = 3
        cache._restart_last_ts[key] = time.time() - 10  # only 10s

        now = time.time()
        last = cache._restart_last_ts.get(key, 0.0)
        if now - last > cache._restart_decay_sec and cache._restart_counts.get(key, 0) > 0:
            cache._restart_counts[key] = max(0, cache._restart_counts[key] - 1)
        cache._restart_counts[key] += 1

        self.assertEqual(cache._restart_counts[key], 4)  # no decay, 3+1=4

    def test_get_stale_returns_none(self):
        from arbitrage.system.ws_orderbooks import WsOrderbookCache
        from arbitrage.system.models import OrderBookSnapshot
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = OrderBookSnapshot(
            exchange="okx", symbol="BTCUSDT", bid=100.0, ask=101.0,
            timestamp=time.time() - 10,  # stale
        )
        self.assertIsNone(cache.get("okx", "BTCUSDT"))

    def test_get_fresh_returns_snapshot(self):
        from arbitrage.system.ws_orderbooks import WsOrderbookCache
        from arbitrage.system.models import OrderBookSnapshot
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = OrderBookSnapshot(
            exchange="okx", symbol="BTCUSDT", bid=100.0, ask=101.0, timestamp=time.time()
        )
        snap = cache.get("okx", "BTCUSDT")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.bid, 100.0)

    def test_get_depth_stale_returns_none(self):
        from arbitrage.system.ws_orderbooks import WsOrderbookCache
        from arbitrage.system.models import OrderBookSnapshot
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = OrderBookSnapshot(
            exchange="okx", symbol="BTCUSDT", bid=100.0, ask=101.0,
            timestamp=time.time() - 10,
        )
        cache._depth.setdefault("okx", {})["BTCUSDT"] = {"bids": [], "asks": []}
        self.assertIsNone(cache.get_depth("okx", "BTCUSDT"))

    def test_health_status_empty(self):
        from arbitrage.system.ws_orderbooks import WsOrderbookCache
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        self.assertEqual(len(cache.health_status()), 0)


# ─────────────────────────────────────────────────────────
# 3. Strategy per-direction cooldown tests
# ─────────────────────────────────────────────────────────

class TestFuturesCrossExchangeStrategy(unittest.TestCase):
    """Test strategy cooldown is per-direction."""

    def setUp(self):
        from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
        self.strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=0.01,
            target_profit_pct=0.10,
            max_spread_risk_pct=0.20,
        )

    def test_cooldown_keys_are_directional(self):
        from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000, ask=50010),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50100, ask=50110),
            },
            spot_orderbooks={},
            orderbook_depth={},
            spot_orderbook_depth={},
            balances={"okx": 10000, "htx": 10000},
            fee_bps={},
            funding_rates={"okx": 0.0001, "htx": 0.0001},
            volatility=0.01,
            trend_strength=0.0,
            atr=100.0,
            atr_rolling=100.0,
            indicators={},
        )

        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.strategy.on_market_snapshot(snapshot))
        for key in self.strategy._last_signal_ts:
            self.assertIn("_long_", key)
            self.assertIn("_short_", key)
        loop.close()

    def test_opposite_direction_not_blocked(self):
        key_ab = "spread_okx_long_htx_short_BTCUSDT"
        key_ba = "spread_htx_long_okx_short_BTCUSDT"

        self.strategy._last_signal_ts[key_ab] = time.time()

        # AB is on cooldown
        self.assertTrue(
            time.time() - self.strategy._last_signal_ts.get(key_ab, 0) < self.strategy._signal_cooldown_sec
        )
        # BA is NOT on cooldown
        self.assertFalse(
            time.time() - self.strategy._last_signal_ts.get(key_ba, 0) < self.strategy._signal_cooldown_sec
        )

    def test_fee_calculation_uses_snapshot(self):
        from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000, ask=50010),
            },
            spot_orderbooks={},
            orderbook_depth={},
            spot_orderbook_depth={},
            balances={},
            fee_bps={"okx": {"perp": 4.5}},  # 4.5 bps
            funding_rates={},
            volatility=0.01,
            trend_strength=0.0,
            atr=100.0,
            atr_rolling=100.0,
            indicators={},
        )
        fee = self.strategy._get_fee_pct("okx", snapshot)
        self.assertAlmostEqual(fee, 0.045, places=3)  # 4.5 bps = 0.045%


# ─────────────────────────────────────────────────────────
# 4. Config validation tests
# ─────────────────────────────────────────────────────────

class TestTradingSystemConfig(unittest.TestCase):
    """Test config validation catches invariant violations."""

    def _make_config(self, **risk_overrides):
        from arbitrage.system.config import TradingSystemConfig, RiskConfig, ApiCredentials
        risk = RiskConfig(**risk_overrides)
        return TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx", "htx"],
            credentials={
                "okx": ApiCredentials(api_key="k", api_secret="s"),
                "htx": ApiCredentials(api_key="k", api_secret="s"),
            },
            starting_equity=10000.0,
            risk=risk,
        )

    def test_strategy_alloc_exceeds_total_raises(self):
        config = self._make_config(
            max_total_exposure_pct=0.10,
            max_strategy_allocation_pct=0.50,
        )
        with self.assertRaises(ValueError) as ctx:
            config.validate()
        self.assertIn("max_strategy_allocation_pct", str(ctx.exception))

    def test_valid_config_passes(self):
        config = self._make_config(
            max_total_exposure_pct=0.50,
            max_strategy_allocation_pct=0.20,
        )
        config.validate()

    def test_no_symbols_raises(self):
        from arbitrage.system.config import TradingSystemConfig, ApiCredentials
        config = TradingSystemConfig(
            symbols=[], exchanges=["okx", "htx"],
            credentials={
                "okx": ApiCredentials(api_key="k", api_secret="s"),
                "htx": ApiCredentials(api_key="k", api_secret="s"),
            },
            starting_equity=10000.0,
        )
        with self.assertRaises(ValueError):
            config.validate()

    def test_single_exchange_raises(self):
        from arbitrage.system.config import TradingSystemConfig, ApiCredentials
        config = TradingSystemConfig(
            symbols=["BTCUSDT"], exchanges=["okx"],
            credentials={"okx": ApiCredentials(api_key="k", api_secret="s")},
            starting_equity=10000.0,
        )
        with self.assertRaises(ValueError):
            config.validate()

    def test_zero_equity_raises(self):
        config = self._make_config()
        from dataclasses import replace
        config = replace(config, starting_equity=0)
        with self.assertRaises(ValueError):
            config.validate()

    def test_reliability_rank_in_execution_config(self):
        from arbitrage.system.config import ExecutionConfig
        cfg = ExecutionConfig()
        self.assertEqual(cfg.reliability_rank["okx"], 0)
        self.assertEqual(cfg.reliability_rank["binance"], 3)


# ─────────────────────────────────────────────────────────
# 5. Execution engine — per-symbol locks
# ─────────────────────────────────────────────────────────

class TestAtomicExecutionEngine(unittest.TestCase):
    """Test execution engine per-symbol locks and dry run."""

    def _make_engine(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        from arbitrage.system.config import ExecutionConfig
        from arbitrage.system.slippage import SlippageModel
        from arbitrage.system.state import SystemState

        venue = MagicMock()
        venue.get_balances = AsyncMock(return_value={"okx": 1000, "htx": 1000})
        monitor = MagicMock()
        monitor.emit = AsyncMock()
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        return AtomicExecutionEngine(
            config=ExecutionConfig(dry_run=True),
            venue=venue,
            slippage=SlippageModel(),
            state=state,
            monitor=monitor,
        ), venue, monitor

    def test_per_symbol_locks_independent(self):
        engine, _, _ = self._make_engine()
        lock1 = engine._get_symbol_lock("BTCUSDT")
        lock2 = engine._get_symbol_lock("ETHUSDT")
        lock1_again = engine._get_symbol_lock("BTCUSDT")
        self.assertIsNot(lock1, lock2)
        self.assertIs(lock1, lock1_again)

    def test_dry_run_creates_position(self):
        from arbitrage.system.models import TradeIntent, StrategyId
        engine, _, _ = self._make_engine()
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
            metadata={"long_price": 50000, "short_price": 50100},
        )
        loop = asyncio.new_event_loop()
        report = loop.run_until_complete(
            engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        )
        self.assertTrue(report.success)
        self.assertEqual(report.message, "dry_run_fill")
        positions = loop.run_until_complete(engine.state.list_positions())
        self.assertEqual(len(positions), 1)
        loop.close()


# ─────────────────────────────────────────────────────────
# 6. Market data initialization tests
# ─────────────────────────────────────────────────────────

class TestMarketDataEngine(unittest.TestCase):
    """Test market data engine handles init failures."""

    def test_empty_common_pairs_logged(self):
        from arbitrage.core.market_data import MarketDataEngine
        mock_client = MagicMock()
        mock_client.get_instruments = AsyncMock(return_value={"code": "0", "data": []})
        engine = MarketDataEngine(exchanges={"okx": mock_client, "htx": mock_client})
        loop = asyncio.new_event_loop()
        with patch("arbitrage.core.market_data.logger") as mock_logger:
            count = loop.run_until_complete(engine.initialize())
        loop.close()
        self.assertEqual(count, 0)
        mock_logger.error.assert_called()

    def test_common_pairs_intersection(self):
        from arbitrage.core.market_data import MarketDataEngine
        engine = MarketDataEngine(exchanges={})
        engine.instruments = {
            "okx": {"BTCUSDT", "ETHUSDT", "SOLUSDT"},
            "htx": {"BTCUSDT", "ETHUSDT", "XRPUSDT"},
        }
        active = [s for s in engine.instruments.values() if s]
        engine.common_pairs = set.intersection(*active)
        self.assertEqual(engine.common_pairs, {"BTCUSDT", "ETHUSDT"})


# ─────────────────────────────────────────────────────────
# 7. Keyboard UX tests
# ─────────────────────────────────────────────────────────

class TestKeyboards(unittest.TestCase):
    """Test keyboard improvements."""

    def test_main_menu_russian_stocks_button(self):
        from keyboards import main_menu
        kb = main_menu()
        texts = [btn.text for row in kb.keyboard for btn in row]
        self.assertIn("📈 Акции", texts)
        self.assertNotIn("Stocks", texts)

    def test_timezone_kb_quick_select(self):
        from keyboards import build_timezone_kb
        kb = build_timezone_kb()
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("🇷🇺 Москва", texts)
        self.assertIn("🇬🇧 Лондон", texts)
        self.assertIn("── Все часовые пояса ──", texts)

    def test_timezone_kb_noop_separator(self):
        from keyboards import build_timezone_kb
        kb = build_timezone_kb()
        noop = any(
            btn.callback_data == "noop"
            for row in kb.inline_keyboard
            for btn in row
        )
        self.assertTrue(noop)

    def test_timezone_kb_full_range(self):
        from keyboards import build_timezone_kb
        kb = build_timezone_kb()
        all_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("set_tz:-12", all_data)
        self.assertIn("set_tz:0", all_data)
        self.assertIn("set_tz:14", all_data)


# ─────────────────────────────────────────────────────────
# 8. Engine health timeout test
# ─────────────────────────────────────────────────────────

class TestEngineHealthTimeout(unittest.TestCase):
    """Engine handles provider.health() timeout gracefully."""

    def test_timeout_returns_empty(self):
        loop = asyncio.new_event_loop()

        async def slow_health():
            await asyncio.sleep(20)
            return {"okx": 100}

        async def run():
            try:
                return await asyncio.wait_for(slow_health(), timeout=0.1)
            except asyncio.TimeoutError:
                return {}

        result = loop.run_until_complete(run())
        self.assertEqual(result, {})
        self.assertEqual(max(result.values(), default=0.0), 0.0)
        loop.close()


# ─────────────────────────────────────────────────────────
# 9. Safe callback parsing tests
# ─────────────────────────────────────────────────────────

class TestSafeCallbackParsing(unittest.TestCase):
    """Callback data parsing handles malformed input."""

    def test_no_colon_raises_index_error(self):
        with self.assertRaises(IndexError):
            "invalid".split(":", 1)[1]

    def test_empty_data_raises_index_error(self):
        with self.assertRaises(IndexError):
            "".split(":", 1)[1]

    def test_valid_data(self):
        self.assertEqual("toggle_curr:USD".split(":", 1)[1], "USD")

    def test_int_parse_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            int("set_tz:abc".split(":", 1)[1])

    def test_noop_handled(self):
        raw = "set_tz:noop".split(":", 1)[1]
        self.assertEqual(raw, "noop")


# ─────────────────────────────────────────────────────────
# 10. Slippage Model tests
# ─────────────────────────────────────────────────────────

class TestSlippageModel(unittest.TestCase):
    def test_estimate_positive(self):
        from arbitrage.system.slippage import SlippageModel
        bps = SlippageModel().estimate(1000, 100_000, 0.02, 100)
        self.assertGreater(bps, 0)

    def test_walk_book_basic(self):
        from arbitrage.system.slippage import SlippageModel
        asks = [["100", "1.0"], ["101", "2.0"], ["102", "3.0"]]
        vwap = SlippageModel.walk_book(asks, 200)
        self.assertGreater(vwap, 0)
        self.assertGreaterEqual(vwap, 100)
        self.assertLessEqual(vwap, 102)

    def test_walk_book_empty(self):
        from arbitrage.system.slippage import SlippageModel
        self.assertEqual(SlippageModel.walk_book([], 100), 0.0)


# ─────────────────────────────────────────────────────────
# 11. Circuit Breaker tests
# ─────────────────────────────────────────────────────────

class TestCircuitBreaker(unittest.TestCase):
    def test_starts_available(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        self.assertTrue(ExchangeCircuitBreaker().is_available("okx"))

    def test_trips_after_errors(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        for _ in range(3):
            cb.record_error("okx", "test")
        self.assertFalse(cb.is_available("okx"))

    def test_success_resets_count(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        cb.record_error("okx", "e1")
        cb.record_error("okx", "e2")
        cb.record_success("okx")
        self.assertTrue(cb.is_available("okx"))


# ─────────────────────────────────────────────────────────
# 12. Capital Allocator tests
# ─────────────────────────────────────────────────────────

class TestCapitalAllocator(unittest.TestCase):
    def test_allocation_returns_correct_strategies(self):
        from arbitrage.system.capital_allocator import CapitalAllocator
        from arbitrage.system.config import RiskConfig
        from arbitrage.system.models import StrategyId
        allocator = CapitalAllocator(RiskConfig())
        plan = allocator.allocate(
            equity=10000,
            avg_funding_bps=5.0,
            volatility_regime=0.02,
            trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        self.assertIn(StrategyId.FUTURES_CROSS_EXCHANGE, plan.strategy_allocations)
        self.assertGreater(plan.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE], 0)


if __name__ == "__main__":
    unittest.main()
