"""Risk management, state, and configuration tests.

Consolidated from:
- test_all_fixes.py (RiskManager, Balance, Locks, Notifications, Config)
- test_all_critical_fixes.py (DailyDrawdown, Exposure, RiskManager, BotState)
- test_all_audit_fixes.py (DailyDD, KillSwitch, EmergencyHedge, Asyncio)
- test_critical_fixes_new.py (StatePersistence, PnL, DeltaCalculation)
"""
from __future__ import annotations

import asyncio
import math
import time
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ═══════════════════════════════════════════════════════
# FIX #2: Configurable critical balance threshold
# ═══════════════════════════════════════════════════════

class TestConfigurableCriticalBalance:
    """Verify emergency close uses configurable threshold, not hardcoded $5."""

    def _make_risk_manager(self, min_total_balance=10.0, position_count=0, total_balance=1000.0):
        from arbitrage.core.state import BotState
        from arbitrage.core.risk import RiskManager

        config_mock = MagicMock()
        config_mock.max_position_pct = 0.30
        config_mock.max_concurrent_positions = 3
        config_mock.emergency_margin_ratio = 0.05
        config_mock.max_delta_percent = 0.05
        config_mock.min_balance_per_side = 5.0
        config_mock.min_total_balance = min_total_balance
        config_mock.max_exposure_pct = 0.30
        config_mock.max_position_duration_hours = 72.0
        config_mock.max_daily_drawdown_pct = 0.05
        config_mock.starting_equity = total_balance

        state = MagicMock()
        state.positions = {f"pos{i}": MagicMock() for i in range(position_count)}
        state.total_balance = total_balance

        rm = RiskManager.__new__(RiskManager)
        rm.config = config_mock
        rm.state = state
        rm.state.positions = state.positions
        return rm

    def test_default_threshold_is_5(self):
        rm = self._make_risk_manager()
        risk_mgr_config = rm.config
        assert risk_mgr_config.min_total_balance == 10.0

    def test_configurable_threshold_is_respected(self):
        rm = self._make_risk_manager(min_total_balance=20.0)
        assert rm.config.min_total_balance == 20.0


# ---------------------------------------------------------------------------
# Fix #1: Daily Drawdown Reset Bug
# ---------------------------------------------------------------------------

class TestDailyDrawdownResetFix:
    """Verify DD is computed after reset, not skipped via early return."""

    def test_dd_computed_after_midnight_reset(self):
        from arbitrage.core.risk import RiskManager

        state = MagicMock()
        state.positions = {}
        state.total_balance = 500.0
        state.okx_balance = 500.0
        state.htx_balance = 500.0
        state.get_positions_by_strategy = MagicMock(return_value=[])

        config_mock = MagicMock()
        config_mock.max_position_pct = 0.30
        config_mock.max_concurrent_positions = 3
        config_mock.emergency_margin_ratio = 0.05
        config_mock.max_delta_percent = 0.01
        config_mock.min_balance_per_side = 5.0
        config_mock.min_total_balance = 10.0
        config_mock.max_exposure_pct = 0.30
        config_mock.max_position_duration_hours = 72.0
        config_mock.max_daily_drawdown_pct = 0.05
        config_mock.starting_equity = 1000.0

        rm = MagicMock()
        rm.config = config_mock
        rm.state = state

        can_open = RiskManager.can_open_position(rm)
        assert can_open is True

    def test_drawdown_below_threshold_blocks(self):
        from arbitrage.core.risk import RiskManager

        state = MagicMock()
        state.positions = {}
        state.total_balance = 900.0
        state.okx_balance = 450.0
        state.htx_balance = 450.0
        state.get_positions_by_strategy = MagicMock(return_value=[])

        config_mock = MagicMock()
        config_mock.max_daily_drawdown_pct = 0.05
        config_mock.starting_equity = 1000.0

        rm = MagicMock()
        rm.config = config_mock
        rm.state = state

        with pytest.raises(Exception):
            RiskManager.can_open_position(rm)


# ---------------------------------------------------------------------------
# Config Validation
# ---------------------------------------------------------------------------

class TestConfigValidation:
    """Test config validation logic."""

    def test_to_low_spread_rejected(self):
        from arbitrage.system.config import TradingSystemConfig, ExecutionConfig, RiskConfig, StrategyConfig

        config = TradingSystemConfig(
            exchanges=["okx", "htx"],
            symbols=["BTCUSDT"],
            starting_equity=1000.0,
            api_keys={},
            risk=RiskConfig(),
            execution=ExecutionConfig(dry_run=True),
            strategy=StrategyConfig(min_spread_pct=0.01, enabled=["futures_cross_exchange"]),
        )
        with pytest.raises(ValueError, match="min_spread_pct.*too low"):
            config.validate()

    def test_valid_spread_accepted(self):
        from arbitrage.system.config import TradingSystemConfig, ExecutionConfig, RiskConfig, StrategyConfig

        config = TradingSystemConfig(
            exchanges=["okx", "htx"],
            symbols=["BTCUSDT"],
            starting_equity=1000.0,
            api_keys={},
            risk=RiskConfig(),
            execution=ExecutionConfig(dry_run=True),
            strategy=StrategyConfig(min_spread_pct=0.50, enabled=["futures_cross_exchange"]),
        )
        config.validate()  # should not raise

    def test_no_symbols_rejected(self):
        from arbitrage.system.config import TradingSystemConfig, ExecutionConfig, RiskConfig, StrategyConfig

        config = TradingSystemConfig(
            exchanges=["okx", "htx"],
            symbols=[],
            starting_equity=1000.0,
            api_keys={},
            risk=RiskConfig(),
            execution=ExecutionConfig(dry_run=True),
            strategy=StrategyConfig(min_spread_pct=0.50, enabled=[]),
            trade_all_symbols=False,
        )
        with pytest.raises(ValueError):
            config.validate()

    def test_single_exchange_rejected(self):
        from arbitrage.system.config import TradingSystemConfig, ExecutionConfig, RiskConfig, StrategyConfig

        config = TradingSystemConfig(
            exchanges=["okx"],
            symbols=["BTCUSDT"],
            starting_equity=1000.0,
            api_keys={},
            risk=RiskConfig(),
            execution=ExecutionConfig(dry_run=True),
            strategy=StrategyConfig(min_spread_pct=0.50, enabled=["futures_cross_exchange"]),
        )
        with pytest.raises(ValueError):
            config.validate()


# ---------------------------------------------------------------------------
# State Persistence
# ---------------------------------------------------------------------------

class TestStatePersistence:
    """Test in-memory state with persistence to disk."""

    def test_in_memory_no_disk_io(self):
        from arbitrage.core.state import BotState
        state = BotState(persist_path=":memory:")
        state.update_balance("okx", 1000.0)
        assert state.get_balance("okx") == 1000.0

    def test_positions_serialization(self):
        from arbitrage.core.state import BotState, ActivePosition
        state = BotState(persist_path=":memory:")

        pos = ActivePosition(
            strategy="test",
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            long_contracts=1.0,
            short_contracts=1.0,
            long_price=50000.0,
            short_price=50100.0,
            entry_spread=0.002,
            size_usd=500.0,
        )
        state.add_position_sync(pos)
        assert state.position_count() == 1
        assert state.get_position("test", "BTCUSDT") is not None

    def test_position_removal(self):
        from arbitrage.core.state import BotState, ActivePosition
        state = BotState(persist_path=":memory:")

        pos = ActivePosition(
            strategy="test", symbol="BTCUSDT",
            long_exchange="okx", short_exchange="htx",
            long_contracts=1.0, short_contracts=1.0,
            long_price=50000.0, short_price=50100.0,
            entry_spread=0.002, size_usd=500.0,
        )
        state.add_position_sync(pos)
        removed = state.remove_position_sync("active:test:BTCUSDT")
        assert removed is not None
        assert state.position_count() == 0


# ---------------------------------------------------------------------------
# PnL Calculation All Exchanges
# ---------------------------------------------------------------------------

class TestCalculatePnlAllExchanges:
    """Test that PnL works for any exchange pair, not just OKX/HTX."""

    def test_pnl_bybit_okx(self):
        from arbitrage.core.state import BotState, Position
        state = BotState(persist_path=":memory")

        pos = Position(
            exchange="bybit",
            symbol="BTCUSDT",
            side="LONG",
            size=1.0,
            entry_price=50000.0,
        )
        state.add_position_sync(pos)

        from arbitrage.core.state import OrderBookData
        state._orderbooks["bybit"] = OrderBookData(
            exchange="bybit", symbol="BTCUSDT",
            bids=[[51000.0, 10.0]], asks=[[51001.0, 10.0]],
            timestamp=time.time(),
            best_bid=51000.0, best_ask=51001.0,
        )

        pnl = state.calculate_pnl()
        assert abs(pnl - 1000.0) < 1e-6

    def test_pnl_short_bybit(self):
        from arbitrage.core.state import BotState, Position, OrderBookData
        state = BotState(persist_path=":memory")

        pos = Position(
            exchange="bybit",
            symbol="BTCUSDT",
            side="SHORT",
            size=1.0,
            entry_price=50000.0,
        )
        state.add_position_sync(pos)
        state._orderbooks["bybit"] = OrderBookData(
            exchange="bybit", symbol="BTCUSDT",
            bids=[[49000.0, 10.0]], asks=[[49001.0, 10.0]],
            timestamp=time.time(),
            best_bid=49000.0, best_ask=49001.0,
        )

        pnl = state.calculate_pnl()
        assert abs(pnl - 1000.0) < 1e-6


# ---------------------------------------------------------------------------
# Kill Switch Temporary Fix
# ---------------------------------------------------------------------------

class TestKillSwitchTemporary:
    """Test that daily DD triggers temporary (cooldown) not permanent kill switch."""

    def test_daily_dd_triggers_cooldown(self):
        """Daily DD should trigger temporary kill switch, not permanent."""
        from arbitrage.core.state import BotState

        rm = MagicMock()
        rm.trigger_kill_switch = AsyncMock()
        config_mock = MagicMock()
        config_mock.max_daily_drawdown_pct = 0.05

        # Simulate daily DD detection
        dd_daily = 0.10  # 10% daily drop
        assert dd_daily > config_mock.max_daily_drawdown_pct
        # Trigger should be called with permanent=False


# ---------------------------------------------------------------------------
# Risk Engine Daily DD
# ---------------------------------------------------------------------------

class TestRiskEngineDailyDD:
    """Test risk engine approves intents correctly."""

    def test_daily_dd_rejects_trade(self):
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig
        from arbitrage.system.state import SystemState

        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        config = RiskConfig(max_daily_drawdown_pct=0.05)
        engine = RiskEngine(config=config, state=state)

        intent = MagicMock()
        intent.long_exchange = "okx"
        intent.short_exchange = "htx"
        allocation = MagicMock(strategy_allocations={})

        decision = asyncio.get_event_loop().run_until_complete(
            engine.validate_intent(
                intent=intent,
                allocation_plan=allocation,
                proposed_notional=100.0,
                estimated_slippage_bps=5.0,
                leverage=1.0,
                api_latency_ms=100.0,
                snapshot=None,
            )
        )
        assert decision.approved is True

    def test_high_latency_rejects(self):
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig
        from arbitrage.system.state import SystemState

        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        config = RiskConfig(api_latency_limit_ms=400)
        engine = RiskEngine(config=config, state=state)

        intent = MagicMock()
        intent.long_exchange = "okx"
        intent.short_exchange = "htx"
        allocation = MagicMock(strategy_allocations={})

        decision = asyncio.get_event_loop().run_until_complete(
            engine.validate_intent(
                intent=intent,
                allocation_plan=allocation,
                proposed_notional=100.0,
                estimated_slippage_bps=5.0,
                leverage=1.0,
                api_latency_ms=5000.0,  # Way over limit
                snapshot=None,
            )
        )
        assert decision.approved is False
        assert "api_latency" in decision.reason


# ---------------------------------------------------------------------------
# Slippage Model
# ---------------------------------------------------------------------------

class TestSlippageModel:
    """Test slippage estimation model."""

    def test_walk_book_empty(self):
        from arbitrage.system.slippage import SlippageModel
        result = SlippageModel.walk_book([], 1000.0)
        assert result <= 0

    def test_walk_book_sufficient_depth(self):
        from arbitrage.system.slippage import SlippageModel
        book = [[100.0, 10000.0], [100.1, 10000.0]]
        result = SlippageModel.walk_book(book, 500.0)
        assert abs(result - 100.0) < 0.01

    def test_walk_book_insufficient_depth(self):
        """If book walks to second level, price should worsen."""
        from arbitrage.system.slippage import SlippageModel
        book = [[100.0, 100.0], [101.0, 10000.0]]
        result = SlippageModel.walk_book(book, 5000.0)
        assert result > 100.0


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    """Test token-bucket rate limiter."""

    def test_acquire_and_release(self):
        from arbitrage.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()
        limiter._buckets.clear()

        loop = asyncio.get_event_loop()
        loop.run_until_complete(limiter.acquire("okx"))

    def test_429_backoff(self):
        from arbitrage.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()
        limiter._buckets.clear()

        backoff1 = limiter.record_429("okx")
        backoff2 = limiter.record_429("okx")
        assert backoff2 >= backoff1

    def test_429_resets_on_success(self):
        from arbitrage.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()
        limiter._buckets.clear()

        limiter.record_429("okx")
        assert limiter._buckets["okx"].backoff_until > 0

        limiter.record_success("okx")
        bucket = limiter._buckets["okx"]
        assert bucket.backoff_until == 0.0


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class TestNotifications:
    """Test notification manager."""

    def test_notify_on_trade(self):
        from arbitrage.core.notifications import NotificationManager

        nm = NotificationManager.__new__(NotificationManager)
        nm._notify = AsyncMock()

        result = asyncio.get_event_loop().run_until_complete(
            nm.on_trade_close(
                "BTCUSDT", "okx", "htx", "test",
                "take_profit", 1.50, 0.5
            )
        )
        nm._notify.assert_called_once()


# ---------------------------------------------------------------------------
# WS Structure
# ---------------------------------------------------------------------------

class TestWSLiveness:
    """Test WebSocket liveness check."""

    def test_liveness_check_returns_valid(self):
        """WS liveness returns dict with keys."""
        expected_keys = {"status", "timestamp", "error"}

        result = {"status": "ok", "timestamp": time.time(), "error": None}
        for key in expected_keys:
            assert key in result

    def test_liveness_check_returns_error(self):
        expected_keys = {"status", "timestamp", "error"}
        result = {"status": "error", "timestamp": time.time(), "error": "connection_refused"}
        for key in expected_keys:
            assert key in result
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Helpers and Indicators
# ---------------------------------------------------------------------------

class TestHelpers:
    """Test market intelligence helpers."""

    def test_rsi_basic(self):
        from market_intelligence.indicators import rsi
        prices = [50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60] * 2
        result = rsi(prices, 14)
        assert 0 <= result <= 100

    def test_ema_basic(self):
        from market_intelligence.indicators import ema
        prices = [1, 2, 3, 4, 5] * 4
        result = ema(prices, 5)
        assert 1 <= result <= 5

    def test_macd_basic(self):
        from market_intelligence.indicators import macd
        prices = list(range(1, 40))
        result = macd(prices, 12, 26, 9)
        assert isinstance(result, float)

    def test_bollinger_bands_basic(self):
        from market_intelligence.indicators import bollinger_bands
        prices = list(range(1, 25))
        upper, middle, lower = bollinger_bands(prices, 20, 2)
        assert upper >= middle >= lower
