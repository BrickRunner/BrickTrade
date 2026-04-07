"""
Comprehensive tests for all audit fixes.
Covers: state, risk, metrics, fees, slippage, circuit_breaker,
        position_sizer, helpers, models, system_state, fee_tier_tracker.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
import json
import tempfile

import pytest


# ═══════════════════════════════════════════════════════════════
# 1. core/state.py — BotState fixes
# ═══════════════════════════════════════════════════════════════

class TestBotState:
    """Tests for BotState: locks, PnL, negative balance rejection."""

    def _make_state(self):
        from arbitrage.core.state import BotState
        return BotState()

    def test_reject_negative_balance(self):
        """Negative balance (failed API fetch sentinel) must be rejected."""
        state = self._make_state()
        state.update_balance_sync("okx", 100.0)
        state.update_balance_sync("okx", -1.0)  # Failed fetch
        assert state.get_balance("okx") == 100.0
        assert state.total_balance == 100.0

    def test_total_balance_never_negative(self):
        """Total balance must never go negative from sentinel values."""
        state = self._make_state()
        state.update_balance_sync("okx", 50.0)
        state.update_balance_sync("htx", 50.0)
        state.update_balance_sync("okx", -1.0)
        assert state.total_balance == 100.0  # Both still positive

    def test_pnl_active_position(self):
        """PnL calculation must include ActivePosition, not just Position."""
        from arbitrage.core.state import BotState, ActivePosition
        state = BotState()

        pos = ActivePosition(
            strategy="arb", symbol="BTCUSDT",
            long_exchange="okx", short_exchange="htx",
            long_contracts=1.0, short_contracts=1.0,
            long_price=50000.0, short_price=50100.0,
            entry_spread=0.2, size_usd=100.0,
        )
        state.add_position_sync(pos)

        # Simulate orderbooks: price moved in our favor
        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.update_orderbook({
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[50200, 10]], "asks": [[50250, 10]],
            "timestamp": time.time(),
        }))
        loop.run_until_complete(state.update_orderbook({
            "exchange": "htx", "symbol": "BTCUSDT",
            "bids": [[49900, 10]], "asks": [[49950, 10]],
            "timestamp": time.time(),
        }))
        loop.close()

        pnl = state.calculate_pnl()
        # Long PnL: (50200 - 50000) * 1.0 = 200
        # Short PnL: (50100 - 49950) * 1.0 = 150
        assert pnl > 0, f"Expected positive PnL, got {pnl}"

    def test_pnl_legacy_position(self):
        """PnL calculation for legacy Position type."""
        from arbitrage.core.state import BotState, Position
        state = BotState()

        pos = Position(exchange="okx", symbol="BTCUSDT", side="LONG",
                       size=1.0, entry_price=50000.0)
        state.add_position_sync(pos)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.update_orderbook({
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[51000, 10]], "asks": [[51050, 10]],
            "timestamp": time.time(),
        }))
        loop.close()

        pnl = state.calculate_pnl()
        assert pnl == pytest.approx(1000.0, rel=0.01)

    def test_async_lock_protects_state(self):
        """Concurrent balance updates must not corrupt state."""
        from arbitrage.core.state import BotState

        async def run():
            state = BotState()
            tasks = []
            for i in range(100):
                exchange = f"ex_{i % 3}"
                tasks.append(state.update_balance(exchange, float(i)))
            await asyncio.gather(*tasks)
            assert state.total_balance >= 0
            assert len(state.balances) == 3

        asyncio.run(run())

    def test_get_all_orderbooks(self):
        """get_all_orderbooks returns all exchanges, not just okx/htx."""
        from arbitrage.core.state import BotState

        async def run():
            state = BotState()
            for ex in ["okx", "htx", "bybit", "binance"]:
                await state.update_orderbook({
                    "exchange": ex, "symbol": "BTCUSDT",
                    "bids": [[50000, 1]], "asks": [[50001, 1]],
                    "timestamp": time.time(),
                })
            all_obs = state.get_all_orderbooks()
            assert len(all_obs) == 4

        asyncio.run(run())

    def test_record_trade_async(self):
        """Record trade with async lock."""
        from arbitrage.core.state import BotState

        async def run():
            state = BotState()
            await state.record_trade(strategy="arb", success=True, pnl=5.0)
            await state.record_trade(strategy="arb", success=False, pnl=-2.0)
            stats = state.get_stats()
            assert stats["total_trades"] == 2
            assert stats["total_pnl"] == pytest.approx(3.0)

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# 2. core/risk.py — RiskManager fixes
# ═══════════════════════════════════════════════════════════════

class TestRiskManager:
    """Tests for RiskManager: ZeroDivision, exposure, circuit breaker."""

    def _make_risk(self):
        from arbitrage.core.risk import RiskManager
        from arbitrage.core.state import BotState
        from unittest.mock import MagicMock

        config = MagicMock()
        config.max_position_pct = 0.5
        config.max_concurrent_positions = 5
        config.emergency_margin_ratio = 0.1
        config.max_delta_percent = 0.5
        config.entry_threshold = 0.5
        config.exit_threshold = 0.1

        state = BotState()
        return RiskManager(config, state), state

    def test_no_zerodiv_on_empty_positions(self):
        """should_emergency_close must not crash with no positions."""
        rm, state = self._make_risk()
        result, reason = rm.should_emergency_close()
        assert result is False

    def test_no_zerodiv_on_flat_positions(self):
        """should_emergency_close handles zero total_notional gracefully."""
        from arbitrage.core.state import ActivePosition
        rm, state = self._make_risk()
        state.update_balance_sync("okx", 100.0)

        pos = ActivePosition(
            strategy="arb", symbol="BTCUSDT",
            long_exchange="okx", short_exchange="htx",
            long_contracts=0, short_contracts=0,
            long_price=0.0, short_price=0.0,
            entry_spread=0.0, size_usd=0.0,
        )
        state.add_position_sync(pos)
        result, reason = rm.should_emergency_close()
        # Should not crash, delta calculation handles 0/0

    def test_circuit_breaker_auto_reset(self):
        """Circuit breaker should auto-reset after timeout."""
        import arbitrage.core.risk as risk_mod
        old_timeout = risk_mod._CIRCUIT_BREAKER_TIMEOUT
        risk_mod._CIRCUIT_BREAKER_TIMEOUT = 0.1  # 100ms for testing

        try:
            rm, state = self._make_risk()
            state.update_balance_sync("okx", 100.0)
            state.update_balance_sync("htx", 100.0)

            # Trip circuit breaker
            for _ in range(5):
                rm.record_failure()

            opp = type("Opp", (), {
                "symbol": "BTCUSDT",
                "long_exchange": "okx",
                "short_exchange": "htx",
            })()

            assert rm.can_open_position(opp) is False

            # Wait for auto-reset
            time.sleep(0.15)
            assert rm.can_open_position(opp) is True
        finally:
            risk_mod._CIRCUIT_BREAKER_TIMEOUT = old_timeout

    def test_validate_spread_nan_inf(self):
        """NaN and Inf spreads must be rejected."""
        rm, _ = self._make_risk()
        assert rm.validate_spread(float("nan"), is_entry=True) is False
        assert rm.validate_spread(float("inf"), is_entry=True) is False
        assert rm.validate_spread(float("-inf"), is_entry=False) is False

    def test_validate_spread_exit_convergence(self):
        """Exit spread validation: spread should be small (converged)."""
        rm, _ = self._make_risk()
        assert rm.validate_spread(0.05, is_entry=False) is True
        assert rm.validate_spread(0.5, is_entry=False) is False

    def test_emergency_close_with_active_positions(self):
        """Delta check with actual ActivePosition data."""
        from arbitrage.core.state import ActivePosition
        rm, state = self._make_risk()
        state.update_balance_sync("okx", 100.0)

        # Imbalanced position
        pos = ActivePosition(
            strategy="arb", symbol="BTCUSDT",
            long_exchange="okx", short_exchange="htx",
            long_contracts=10.0, short_contracts=1.0,
            long_price=50000.0, short_price=50000.0,
            entry_spread=0.1, size_usd=100.0,
        )
        state.add_position_sync(pos)
        result, reason = rm.should_emergency_close()
        assert result is True
        assert "delta_exceeded" in reason


# ═══════════════════════════════════════════════════════════════
# 3. core/metrics.py — MetricsTracker fixes
# ═══════════════════════════════════════════════════════════════

class TestMetricsTracker:
    """Tests for MetricsTracker: thread safety, dynamic Sharpe."""

    def test_sharpe_requires_min_trades(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        assert mt.sharpe_ratio() == 0.0

    def test_sharpe_dynamic_annualization(self):
        """Sharpe should use actual trade frequency, not hardcoded 3/day."""
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()

        # Record 10 trades in 1 second (very high frequency)
        for i in range(10):
            mt.record_exit_sync("arb", "BTC", pnl=0.1 + i * 0.01, reason="test")
            mt._trade_timestamps.append(time.time() + i * 0.1)

        sharpe = mt.sharpe_ratio()
        # Should be a real number (not NaN/Inf)
        assert not math.isnan(sharpe)
        assert not math.isinf(sharpe)

    def test_drawdown_tracking(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        mt.record_exit_sync("arb", "BTC", 10.0, "win")
        mt.record_exit_sync("arb", "BTC", -5.0, "loss")
        assert mt._max_drawdown == 5.0
        assert mt._cumulative_pnl == 5.0

    def test_summary(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        mt.record_exit_sync("arb", "BTC", 10.0, "win")
        mt.record_exit_sync("arb", "BTC", -3.0, "loss")
        s = mt.summary()
        assert s["exits"] == 2
        assert s["cumulative_pnl"] == 7.0
        assert s["per_strategy"]["arb"]["wins"] == 1

    def test_async_record(self):
        from arbitrage.core.metrics import MetricsTracker

        async def run():
            mt = MetricsTracker()
            await mt.record_entry("arb", "BTC")
            await mt.record_exit("arb", "BTC", 5.0, "test")
            s = mt.summary()
            assert s["entries"] == 1
            assert s["exits"] == 1

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# 4. system/fees.py — Fee calculation fixes
# ═══════════════════════════════════════════════════════════════

class TestFees:
    """Tests for fees.py: env integration, empty string, fallback."""

    def test_default_fee_is_conservative(self):
        """Default fee should be 5 bps, not 0."""
        from arbitrage.system.fees import fee_bps
        # Remove any env overrides
        for key in list(os.environ):
            if key.startswith("FEE_BPS_"):
                del os.environ[key]
        result = fee_bps("unknown_exchange", "PERP")
        assert result == 5.0

    def test_env_override(self):
        from arbitrage.system.fees import fee_bps
        os.environ["FEE_BPS_TESTEX_PERP"] = "3.5"
        try:
            assert fee_bps("testex", "PERP") == 3.5
        finally:
            del os.environ["FEE_BPS_TESTEX_PERP"]

    def test_empty_string_env_falls_through(self):
        """Empty string in env must NOT return 0, must fall through."""
        from arbitrage.system.fees import fee_bps
        os.environ["FEE_BPS_EMPTYEX_PERP"] = ""
        try:
            result = fee_bps("emptyex", "PERP")
            assert result != 0.0  # Must not be zero
        finally:
            del os.environ["FEE_BPS_EMPTYEX_PERP"]

    def test_round_trip_fees(self):
        from arbitrage.system.fees import total_round_trip_fee_bps
        os.environ["FEE_BPS_OKX_PERP"] = "5.0"
        os.environ["FEE_BPS_HTX_PERP"] = "5.0"
        try:
            total = total_round_trip_fee_bps("okx", "htx", "PERP")
            assert total == 20.0  # 5 * 4 legs
        finally:
            del os.environ["FEE_BPS_OKX_PERP"]
            del os.environ["FEE_BPS_HTX_PERP"]

    def test_fee_bps_from_snapshot(self):
        from arbitrage.system.fees import fee_bps_from_snapshot
        from unittest.mock import MagicMock
        snap = MagicMock()
        snap.fee_bps = {"okx": {"PERP": 3.0, "PERP:BTCUSDT": 2.5}}
        assert fee_bps_from_snapshot(snap, "okx", "PERP", "BTCUSDT") == 2.5
        assert fee_bps_from_snapshot(snap, "okx", "PERP") == 3.0


# ═══════════════════════════════════════════════════════════════
# 5. system/models.py — TradeIntent notional_usd
# ═══════════════════════════════════════════════════════════════

class TestModels:
    def test_trade_intent_has_notional_usd(self):
        from arbitrage.system.models import TradeIntent, StrategyId
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="buy",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
            notional_usd=100.0,
        )
        assert intent.notional_usd == 100.0

    def test_trade_intent_default_notional(self):
        from arbitrage.system.models import TradeIntent, StrategyId
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="buy",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=50.0,
        )
        assert intent.notional_usd == 0.0


# ═══════════════════════════════════════════════════════════════
# 6. system/slippage.py — Non-linear model, validation
# ═══════════════════════════════════════════════════════════════

class TestSlippage:
    def test_zero_notional_returns_zero(self):
        from arbitrage.system.slippage import SlippageModel
        sm = SlippageModel()
        assert sm.estimate(0.0, 1000.0, 0.01, 100.0) == 0.0

    def test_zero_depth_returns_high_penalty(self):
        from arbitrage.system.slippage import SlippageModel
        sm = SlippageModel()
        result = sm.estimate(100.0, 0.0, 0.01, 100.0)
        assert result == 500.0

    def test_nonlinear_slippage(self):
        """Larger orders should have superlinear slippage."""
        from arbitrage.system.slippage import SlippageModel
        sm = SlippageModel()
        small = sm.estimate(100.0, 1000.0, 0.01, 50.0)
        big = sm.estimate(500.0, 1000.0, 0.01, 50.0)
        # Big order should have more than 5x the depth component
        ratio = big / small
        assert ratio > 1.5, f"Expected superlinear ratio, got {ratio}"

    def test_result_never_negative(self):
        from arbitrage.system.slippage import SlippageModel
        sm = SlippageModel()
        result = sm.estimate(10.0, 100000.0, 0.0, 0.0)
        assert result >= 0

    def test_walk_book_basic(self):
        from arbitrage.system.slippage import SlippageModel
        levels = [(100.0, 1.0), (101.0, 1.0), (102.0, 1.0)]
        avg = SlippageModel.walk_book(levels, 200.0)
        # Should fill 1@100 + 1@101 = 200 USD for 2 units
        assert avg == pytest.approx(100.5, rel=0.01)

    def test_walk_book_empty(self):
        from arbitrage.system.slippage import SlippageModel
        assert SlippageModel.walk_book([], 100.0) == 0.0

    def test_walk_book_slippage_bps(self):
        from arbitrage.system.slippage import SlippageModel
        levels = [(100.0, 1.0), (101.0, 1.0)]
        bps = SlippageModel.walk_book_slippage_bps(levels, 200.0, 100.0)
        assert bps > 0


# ═══════════════════════════════════════════════════════════════
# 7. system/circuit_breaker.py — Error severity
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_transient_errors_half_weight(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=5)
        for _ in range(9):
            cb.record_error("okx", "connection timeout")  # transient = 0.5 each
        # 9 * 0.5 = 4.5, still below 5
        assert cb.is_available("okx") is True
        cb.record_error("okx", "connection timeout")  # 10 * 0.5 = 5, trips
        assert cb.is_available("okx") is False

    def test_fatal_error_immediate_trip(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=5)
        cb.record_error("htx", "insufficient_margin for order")
        assert cb.is_available("htx") is False

    def test_normal_error_counting(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3)
        cb.record_error("okx", "unknown error", severity="normal")
        cb.record_error("okx", "unknown error", severity="normal")
        assert cb.is_available("okx") is True
        cb.record_error("okx", "unknown error", severity="normal")
        assert cb.is_available("okx") is False

    def test_success_resets_counter(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3)
        cb.record_error("okx", "err1")
        cb.record_error("okx", "err2")
        cb.record_success("okx")
        cb.record_error("okx", "err3")
        assert cb.is_available("okx") is True

    def test_cooldown_expiry(self):
        from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=0.1)
        cb.record_error("okx", "err", severity="normal")
        assert cb.is_available("okx") is False
        time.sleep(0.15)
        assert cb.is_available("okx") is True


# ═══════════════════════════════════════════════════════════════
# 8. system/state.py — SystemState fixes
# ═══════════════════════════════════════════════════════════════

class TestSystemState:
    def test_kill_switch_permanent_survives_daily_reset(self):
        """Permanent kill switch must NOT auto-reset on new day."""
        from arbitrage.system.state import SystemState

        async def run():
            state = SystemState(starting_equity=1000.0, positions_file=":memory:")
            await state.trigger_kill_switch(permanent=True)
            # Simulate new day
            from datetime import date, timedelta
            state._daily_reset_date = date.today() - timedelta(days=1)
            state._maybe_reset_daily()
            # Permanent kill switch should still be active
            assert await state.kill_switch_triggered() is True

        asyncio.run(run())

    def test_non_permanent_kill_switch_resets_daily(self):
        from arbitrage.system.state import SystemState

        async def run():
            state = SystemState(starting_equity=1000.0, positions_file=":memory:")
            await state.trigger_kill_switch(permanent=False)
            assert await state.kill_switch_triggered() is True
            # Simulate new day
            from datetime import date, timedelta
            state._daily_reset_date = date.today() - timedelta(days=1)
            state._maybe_reset_daily()
            assert await state.kill_switch_triggered() is False

        asyncio.run(run())

    def test_equity_history_trimmed(self):
        from arbitrage.system.state import SystemState

        async def run():
            state = SystemState(starting_equity=1000.0, positions_file=":memory:")
            for i in range(15_000):
                await state.set_equity(1000.0 + i * 0.01)
            assert len(state._history) <= state._MAX_HISTORY_LEN

        asyncio.run(run())

    def test_position_persistence(self):
        from arbitrage.system.state import SystemState
        from arbitrage.system.models import OpenPosition, StrategyId

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_path = f.name

        try:
            async def run():
                state = SystemState(starting_equity=1000.0, positions_file=tmp_path)
                pos = OpenPosition(
                    position_id="test-1",
                    strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                    symbol="BTCUSDT",
                    long_exchange="okx",
                    short_exchange="htx",
                    notional_usd=50.0,
                    entry_mid=50000.0,
                    stop_loss_bps=100.0,
                )
                await state.add_position(pos)
                # Wait for async persist
                await asyncio.sleep(0.2)
                assert os.path.exists(tmp_path)

                # Reload
                state2 = SystemState(starting_equity=1000.0, positions_file=tmp_path)
                positions = await state2.list_positions()
                assert len(positions) == 1
                assert positions[0].position_id == "test-1"

            asyncio.run(run())
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_manual_reset_kill_switch(self):
        from arbitrage.system.state import SystemState

        async def run():
            state = SystemState(starting_equity=1000.0, positions_file=":memory:")
            await state.trigger_kill_switch(permanent=True)
            assert await state.kill_switch_triggered() is True
            await state.reset_kill_switch()
            assert await state.kill_switch_triggered() is False

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# 9. utils/helpers.py — Spread & round_down fixes
# ═══════════════════════════════════════════════════════════════

class TestHelpers:
    def test_spread_positive_arb(self):
        """Positive spread = arbitrage opportunity."""
        from arbitrage.utils.helpers import calculate_spread
        spread = calculate_spread(bid_price=50100, ask_price=50000)
        assert spread == pytest.approx(0.2, rel=0.01)

    def test_spread_zero_prices(self):
        from arbitrage.utils.helpers import calculate_spread
        assert calculate_spread(0.0, 100.0) == 0.0
        assert calculate_spread(100.0, 0.0) == 0.0
        assert calculate_spread(0.0, 0.0) == 0.0

    def test_spread_negative_ask(self):
        from arbitrage.utils.helpers import calculate_spread
        assert calculate_spread(100.0, -1.0) == 0.0

    def test_round_down_normal(self):
        from arbitrage.utils.helpers import round_down
        assert round_down(1.999, 2) == 1.99
        assert round_down(1.001, 2) == 1.00

    def test_round_down_negative_decimals(self):
        """Negative decimals should be clamped to 0."""
        from arbitrage.utils.helpers import round_down
        result = round_down(123.456, -1)
        assert result == 123.0

    def test_validate_orderbook(self):
        from arbitrage.utils.helpers import validate_orderbook
        assert validate_orderbook({"bids": [[100, 1]], "asks": [[101, 1]]}) is True
        assert validate_orderbook({"bids": [[100, 1]], "asks": [[99, 1]]}) is False
        assert validate_orderbook({}) is False
        assert validate_orderbook(None) is False


# ═══════════════════════════════════════════════════════════════
# 10. system/position_sizer.py — Locked margin
# ═══════════════════════════════════════════════════════════════

class TestPositionSizer:
    def test_locked_margin_reduces_size(self):
        from arbitrage.system.position_sizer import DynamicPositionSizer
        sizer = DynamicPositionSizer(base_notional_usd=50.0)

        # Without locked margin
        f1 = sizer.calculate_size(
            "BTCUSDT", "okx", "htx", 0.01, 10000.0, 15.0,
            {"okx": 100.0, "htx": 100.0}, 0, 5,
        )

        # With locked margin
        f2 = sizer.calculate_size(
            "BTCUSDT", "okx", "htx", 0.01, 10000.0, 15.0,
            {"okx": 100.0, "htx": 100.0}, 0, 5,
            locked_margin={"okx": 80.0, "htx": 80.0},
        )

        assert f2.final_notional < f1.final_notional

    def test_zero_balance_returns_min(self):
        from arbitrage.system.position_sizer import DynamicPositionSizer
        sizer = DynamicPositionSizer(base_notional_usd=50.0, min_notional_usd=5.0)
        factors = sizer.calculate_size(
            "BTCUSDT", "okx", "htx", 0.01, 10000.0, 15.0,
            {"okx": 0.0, "htx": 0.0}, 0, 5,
        )
        assert factors.final_notional == 5.0

    def test_kelly_criterion_edge_cases(self):
        from arbitrage.system.position_sizer import DynamicPositionSizer
        sizer = DynamicPositionSizer()
        # Edge: win_rate = 0 or 1
        assert sizer.calculate_kelly_size(0.0, 0.1, 0.1, 1000.0) == sizer.base_notional
        assert sizer.calculate_kelly_size(1.0, 0.1, 0.1, 1000.0) == sizer.base_notional
        # Negative edge
        assert sizer.calculate_kelly_size(0.3, 0.01, 0.05, 1000.0) == sizer.min_notional


# ═══════════════════════════════════════════════════════════════
# 11. system/fee_tier_tracker.py — Monthly volume reset
# ═══════════════════════════════════════════════════════════════

class TestFeeTierTracker:
    def test_volume_resets_monthly(self):
        from arbitrage.system.fee_tier_tracker import FeeTierTracker
        from datetime import datetime

        tracker = FeeTierTracker()
        tracker.record_trade_volume("okx", 1000.0)
        assert tracker._volume_tracker["okx"] == 1000.0

        # Simulate next month
        current_month = datetime.now().month
        next_month = (current_month % 12) + 1
        tracker._volume_reset_month["okx"] = next_month  # Make it think we're in a different month
        # Force a different month scenario
        tracker._volume_reset_month["okx"] = (current_month + 1) % 12 or 12
        tracker.record_trade_volume("okx", 500.0)
        # Volume should have reset (old 1000 gone, new 500)
        assert tracker._volume_tracker["okx"] == 500.0

    def test_update_tier(self):
        from arbitrage.system.fee_tier_tracker import FeeTierTracker

        async def run():
            tracker = FeeTierTracker()
            tier = await tracker.update_tier("okx", 3_000_000)
            assert tier.tier_level == 2
            assert tier.taker_fee_bps == 3.5
            assert tier.next_tier_volume == 10_000_000

        asyncio.run(run())

    def test_breakeven_spread(self):
        from arbitrage.system.fee_tier_tracker import FeeTierTracker

        async def run():
            tracker = FeeTierTracker()
            await tracker.update_tier("okx", 0)
            await tracker.update_tier("htx", 0)
            spread = tracker.calculate_breakeven_spread("okx", "htx")
            # Level 0: OKX taker=5, HTX taker=5
            # Entry: 5+5, Exit: 5+5 = 20 + 2 buffer = 22
            assert spread == 22.0

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# 12. system/risk.py — RiskEngine
# ═══════════════════════════════════════════════════════════════

class TestRiskEngine:
    def test_kill_switch_blocks_all(self):
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig
        from arbitrage.system.state import SystemState
        from arbitrage.system.models import TradeIntent, AllocationPlan, StrategyId

        async def run():
            config = RiskConfig()
            state = SystemState(starting_equity=1000.0, positions_file=":memory:")
            engine = RiskEngine(config=config, state=state)

            await state.trigger_kill_switch()

            intent = TradeIntent(
                strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                symbol="BTCUSDT", long_exchange="okx", short_exchange="htx",
                side="buy", confidence=0.9, expected_edge_bps=15.0,
                stop_loss_bps=50.0, notional_usd=10.0,
            )
            plan = AllocationPlan(
                strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 100.0},
                total_allocatable_capital=500.0,
            )
            decision = await engine.validate_intent(
                intent, plan, 10.0, 2.0, 5.0, 50.0,
            )
            assert decision.approved is False
            assert "kill_switch" in decision.reason

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# 13. Integration: execution.py has no undefined variables
# ═══════════════════════════════════════════════════════════════

class TestExecutionImport:
    def test_execution_module_imports_clean(self):
        """Verify execution.py can be imported without errors."""
        from arbitrage.system.execution import AtomicExecutionEngine
        assert AtomicExecutionEngine is not None

    def test_execution_report_hedged_field(self):
        from arbitrage.system.models import ExecutionReport
        report = ExecutionReport(
            success=False, position_id=None,
            fill_price_long=0.0, fill_price_short=0.0,
            notional_usd=10.0, slippage_bps=2.0,
            message="test", hedged=True,
        )
        assert report.hedged is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
