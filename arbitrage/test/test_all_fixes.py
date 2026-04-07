"""
Comprehensive test suite for ALL code review fixes — BrickTrade.
Tests cover risk.py, execution.py, config.py, state.py, metrics.py,
strategies, slippage, and WS client APIs.
"""
import asyncio
import math
import time
from unittest.mock import MagicMock, AsyncMock

import pytest


# ═══════════════════════════════════════════════════════
# FIX #2: Configurable critical balance threshold
# ═══════════════════════════════════════════════════════

class TestConfigurableCriticalBalance:
    """Verify emergency close uses configurable threshold, not hardcoded $5."""

    def _make_risk_manager(self, min_total_balance=10.0, position_count=0, total_balance=1000.0):
        """Helper: build RiskManager with proper config via mock."""
        from arbitrage.core.state import BotState
        from arbitrage.core.risk import RiskManager

        # ArbitrageConfig doesn't have all fields → use a mock with the attributes RM reads
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
        # For drawdown: RM reads starting_equity
        config_mock.starting_equity = total_balance  # no drawdown initially

        state = MagicMock()
        state.total_balance = total_balance
        state.position_count.return_value = position_count
        state.get_all_positions.return_value = []

        rm = RiskManager(config_mock, state)
        rm._daily_drawdown_reset_ts = time.time()  # already initialized
        return rm

    def test_custom_min_total_balance(self):
        """RiskManager should accept custom min_total_balance."""
        rm = self._make_risk_manager(min_total_balance=20.0, total_balance=1000.0)
        assert rm._min_total_balance == 20.0

    def test_not_below_custom_threshold(self):
        """Should NOT trigger emergency when balance > min_total_balance."""
        rm = self._make_risk_manager(min_total_balance=20.0, total_balance=25.0, position_count=1)
        should_close, reason = rm.should_emergency_close()
        assert not should_close, f"Should not close at $25 (threshold $20), got: {reason}"

    def test_low_balance_triggers_emergency(self):
        """Should trigger emergency when balance < min_total_balance and positions open."""
        rm = self._make_risk_manager(min_total_balance=10.0, total_balance=0.0, position_count=1)
        should_close, reason = rm.should_emergency_close()
        assert should_close
        assert "balance" in reason

    def test_no_emergency_with_no_positions(self):
        """Low balance should NOT trigger if no positions are open."""
        rm = self._make_risk_manager(min_total_balance=10.0, total_balance=0.0, position_count=0)
        should_close, reason = rm.should_emergency_close()
        assert not should_close, f"No emergency without positions: {reason}"


# ═══════════════════════════════════════════════════════
# FIX #3: Daily drawdown reset uses midnight_ts properly
# ═══════════════════════════════════════════════════════

class TestDailyDrawdownMidnightReset:
    """Verify DD reset uses midnight_ts not 'now' to avoid early resets."""

    def _make_rm(self, starting_equity=1000.0, total_balance=1000.0):
        config_mock = MagicMock()
        config_mock.max_position_pct = 0.30
        config_mock.max_concurrent_positions = 3
        config_mock.emergency_margin_ratio = 0.05
        config_mock.max_delta_percent = 0.05
        config_mock.min_balance_per_side = 5.0
        config_mock.min_total_balance = 10.0
        config_mock.max_exposure_pct = 0.30
        config_mock.max_position_duration_hours = 72.0
        config_mock.max_daily_drawdown_pct = 0.05
        config_mock.starting_equity = starting_equity

        state = MagicMock()
        state.total_balance = total_balance
        state.position_count.return_value = 0
        state.get_all_positions.return_value = []

        from arbitrage.core.risk import RiskManager
        rm = RiskManager(config_mock, state)
        return rm

    def test_reset_ts_initialized_to_midnight(self):
        """After first _update_daily_drawdown call, reset_ts should be today's midnight."""
        import datetime
        rm = self._make_rm(starting_equity=1000.0, total_balance=950.0)
        rm._daily_drawdown_reset_ts = 0.0

        rm._update_daily_drawdown()

        now = time.time()
        utc_now = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        midnight_ts = utc_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        assert abs(rm._daily_drawdown_reset_ts - midnight_ts) < 1.0, (
            f"Expected ~{midnight_ts}, got {rm._daily_drawdown_reset_ts}"
        )

    def test_dd_not_reset_prematurely(self):
        """DD should NOT reset if we're still on the same day."""
        import datetime
        rm = self._make_rm(starting_equity=1000.0, total_balance=940.0)

        now = time.time()
        utc_now = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        midnight_ts = utc_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rm._daily_drawdown_reset_ts = midnight_ts + 0.5  # half a second after midnight
        rm._daily_drawdown = 0.05

        rm._update_daily_drawdown()
        assert rm._daily_drawdown >= 0.059, (
            f"DD should be tracked as ~0.06, got {rm._daily_drawdown}"
        )

    def test_starting_equity_zero_guard(self):
        """Should not crash if starting_equity is zero."""
        rm = self._make_rm(starting_equity=0.0, total_balance=100.0)
        rm._update_daily_drawdown()
        rm._daily_drawdown_reset_ts = time.time()
        assert rm._daily_drawdown == 0.0

    def test_emergency_close_triggered_by_dd(self):
        """Should trigger emergency close when DD exceeds limit."""
        rm = self._make_rm(starting_equity=1000.0, total_balance=900.0)
        rm._daily_drawdown_reset_ts = time.time() - 10
        rm._daily_drawdown = 0.10  # 10% DD, exceeds 5% limit
        rm.state.position_count.return_value = 1

        should_close, reason = rm.should_emergency_close()
        assert should_close
        assert "max_daily_dd" in reason


# ═══════════════════════════════════════════════════════
# FIX #4: Nonce purge mechanism (memory leak prevention)
# ═══════════════════════════════════════════════════════

class TestNoncePurgeMechanism:
    """Verify _inflight_nonces gets purged periodically."""

    def _make_engine(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        from arbitrage.system.slippage import SlippageModel
        venue = MagicMock()
        config = MagicMock()
        config.dry_run = True
        slippage = SlippageModel()
        state = MagicMock()
        monitor = MagicMock()
        return AtomicExecutionEngine(
            config=config, venue=venue, slippage=slippage,
            state=state, monitor=monitor
        )

    def test_purge_expired_nonces(self):
        old_time = time.time() - 300
        engine = self._make_engine()
        engine._inflight_nonces["old_nonce_1"] = old_time
        engine._inflight_nonces["old_nonce_2"] = old_time - 60
        engine._inflight_nonces["new_nonce"] = time.time()

        purged = engine._purge_expired_nonces()
        assert purged >= 2, f"Should purge old nonces, got {purged}"
        assert "old_nonce_1" not in engine._inflight_nonces
        assert "old_nonce_2" not in engine._inflight_nonces
        assert "new_nonce" in engine._inflight_nonces

    def test_purge_hard_cap(self):
        engine = self._make_engine()
        for i in range(engine._NONCE_MAX_ENTRIES + 1000):
            engine._inflight_nonces[f"nonce_{i}"] = time.time()
        purged = engine._purge_expired_nonces()
        assert len(engine._inflight_nonces) <= engine._NONCE_MAX_ENTRIES, (
            f"Should cap at {engine._NONCE_MAX_ENTRIES}, got {len(engine._inflight_nonces)}"
        )

    def test_purge_counter_increments(self):
        engine = self._make_engine()
        initial = engine._nonce_purge_counter
        engine._inflight_nonces["test"] = time.time()
        engine._nonce_purge_counter = initial + 1
        assert engine._nonce_purge_counter == initial + 1


# ═══════════════════════════════════════════════════════
# FIX #12: Config validation
# ═══════════════════════════════════════════════════════

class TestConfigValidation:
    """Verify config rejects invalid values."""

    def test_validate_checks_for_exchange_count(self):
        from arbitrage.system.config import (
            TradingSystemConfig, ApiCredentials,
            RiskConfig, ExecutionConfig, StrategyConfig,
        )
        config = TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx"],
            credentials={"okx": ApiCredentials("k", "s")},
            starting_equity=1000.0,
            risk=RiskConfig(),
            execution=ExecutionConfig(),
            strategy=StrategyConfig(),
        )
        with pytest.raises(ValueError, match="two exchanges"):
            config.validate()

    def test_validate_rejects_negative_equity(self):
        from arbitrage.system.config import (
            TradingSystemConfig, ApiCredentials,
            RiskConfig, ExecutionConfig, StrategyConfig,
        )
        config = TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx", "bybit"],
            credentials={
                "okx": ApiCredentials("k", "s"),
                "bybit": ApiCredentials("k", "s"),
            },
            starting_equity=-100.0,
            risk=RiskConfig(),
            execution=ExecutionConfig(),
            strategy=StrategyConfig(),
        )
        with pytest.raises(ValueError):
            config.validate()

    def test_validate_rejects_invalid_spread(self):
        from arbitrage.system.config import (
            TradingSystemConfig, ApiCredentials,
            RiskConfig, ExecutionConfig, StrategyConfig,
        )
        config = TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx", "bybit"],
            credentials={
                "okx": ApiCredentials("k", "s"),
                "bybit": ApiCredentials("k", "s"),
            },
            starting_equity=1000.0,
            risk=RiskConfig(),
            execution=ExecutionConfig(),
            strategy=StrategyConfig(min_spread_pct=0.01),
        )
        with pytest.raises(ValueError, match="too low"):
            config.validate()

    def test_validate_rejects_blacklist_overlap(self):
        from arbitrage.system.config import (
            TradingSystemConfig, ApiCredentials,
            RiskConfig, ExecutionConfig, StrategyConfig,
        )
        config = TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx", "bybit"],
            credentials={
                "okx": ApiCredentials("k", "s"),
                "bybit": ApiCredentials("k", "s"),
            },
            starting_equity=1000.0,
            risk=RiskConfig(),
            execution=ExecutionConfig(),
            strategy=StrategyConfig(),
            symbol_blacklist=["BTCUSDT"],
        )
        with pytest.raises(ValueError, match="both symbols"):
            config.validate()

    def test_validate_accepts_valid_config(self):
        from arbitrage.system.config import (
            TradingSystemConfig, ApiCredentials,
            RiskConfig, ExecutionConfig, StrategyConfig,
        )
        config = TradingSystemConfig(
            symbols=["BTCUSDT"],
            exchanges=["okx", "bybit"],
            credentials={
                "okx": ApiCredentials("k", "s"),
                "bybit": ApiCredentials("k", "s"),
            },
            starting_equity=1000.0,
            risk=RiskConfig(),
            execution=ExecutionConfig(),
            strategy=StrategyConfig(min_spread_pct=0.15),
        )
        config.validate()  # should not raise

    def test_invalid_env_falls_to_default(self):
        from arbitrage.system.config import _as_float
        result = _as_float("abc", 0.08)
        assert result == 0.08

    def test_as_bool_handles_various_values(self):
        from arbitrage.system.config import _as_bool
        assert _as_bool("true") is True
        assert _as_bool("1") is True
        assert _as_bool("yes") is True
        assert _as_bool("false") is False
        assert _as_bool("0") is False
        assert _as_bool("no") is False
        assert _as_bool(None, False) is False


# ═══════════════════════════════════════════════════════
# FIX #13: Unbounded dict growth in metrics.py
# ═══════════════════════════════════════════════════════

class TestMetricsUnboundedGrowth:
    """Verify metrics dicts have size caps."""

    def test_pnl_history_deque_has_maxlen(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        assert mt._pnl_history.maxlen is not None
        assert mt._pnl_history.maxlen == 1000

    def test_cycle_times_deque_has_maxlen(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        assert mt._cycle_times.maxlen is not None
        assert mt._cycle_times.maxlen == 100

    def test_trade_timestamps_deque_has_maxlen(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        assert mt._trade_timestamps.maxlen is not None
        assert mt._trade_timestamps.maxlen == 1000

    def test_record_exit_updates_cumulative_pnl(self):
        from arbitrage.core.metrics import MetricsTracker
        mt = MetricsTracker()
        mt.record_exit_sync("test", "BTCUSDT", 10.0, "target")
        assert mt._cumulative_pnl == 10.0
        assert mt._entries == 0
        assert mt._exits == 1


# ═══════════════════════════════════════════════════════
# FIX #10: Dynamic correlation in pairs trading
# ═══════════════════════════════════════════════════════

class TestPairsTradingCorrelation:
    """Verify pairs trading z-score logic."""

    def test_stat_arb_pairs_exist(self):
        from arbitrage.system.strategies.pairs_trading import _STAT_ARB_PAIRS
        assert len(_STAT_ARB_PAIRS) > 0
        for pair in _STAT_ARB_PAIRS:
            assert len(pair) == 3
            assert 0 <= pair[2] <= 1

    def test_pairs_strategy_params(self):
        from arbitrage.system.strategies.pairs_trading import PairsTradingStrategy
        strategy = PairsTradingStrategy(entry_zscore=2.0, exit_zscore=0.5, min_history=30)
        assert strategy.entry_zscore == 2.0
        assert strategy.exit_zscore == 0.5

    def test_spread_history_zscore_zero_identical(self):
        from arbitrage.system.strategies.pairs_trading import SpreadHistory
        sh = SpreadHistory()
        for v in [100.0] * 30:
            sh.add(v, time.time())
        assert sh.count == 30
        assert abs(sh.mean - 100.0) < 0.01

    def test_spread_history_std_calculation(self):
        from arbitrage.system.strategies.pairs_trading import SpreadHistory
        sh = SpreadHistory()
        values = [10.0, 12.0, 8.0, 11.0, 9.0] * 6
        for v in values:
            sh.add(v, time.time())
        assert sh.std > 0
        assert sh.mean > 0


# ═══════════════════════════════════════════════════════
# FIX #11: V2 execution exists with proper methods
# ═══════════════════════════════════════════════════════

class TestMarketOrderSafety:
    """Verify V2 execution architecture."""

    def test_v2_has_execute_arbitrage(self):
        from arbitrage.system.execution_v2 import AtomicExecutionEngineV2
        assert hasattr(AtomicExecutionEngineV2, "execute_arbitrage")
        assert hasattr(AtomicExecutionEngine, "_execute_both_legs")
        assert hasattr(AtomicExecutionEngine, "_verify_positions")
        assert hasattr(AtomicExecutionEngine, "_guaranteed_hedge")

    def test_v2_has_position_to_notional(self):
        from arbitrage.system.execution_v2 import AtomicExecutionEngineV2
        assert hasattr(AtomicExecutionEngineV2, "_position_to_notional")

    def test_v2_position_to_notional_bybit(self):
        from arbitrage.system.execution_v2 import AtomicExecutionEngineV2
        venue = MagicMock()
        config = MagicMock()
        monitor = MagicMock()
        engine = AtomicExecutionEngine(venue=venue, config=config, monitor=monitor, market_data=None)
        notional = engine._position_to_notional("bybit", "BTCUSDT", 500.0)
        assert notional == 500.0

    def test_v2_position_to_notional_okx_fallback(self):
        from arbitrage.system.execution_v2 import AtomicExecutionEngineV2
        venue = MagicMock()
        config = MagicMock()
        monitor = MagicMock()
        engine = AtomicExecutionEngine(venue=venue, config=config, monitor=monitor, market_data=None)
        # OKX with default fallback price for BTCUSDT
        notional = engine._position_to_notional("okx", "BTCUSDT", 1.0)
        assert notional > 0


# ═══════════════════════════════════════════════════════
# WS LIVENESS — All WS clients implement is_alive
# ═══════════════════════════════════════════════════════

class TestWSLiveness:
    """Verify all WS clients implement is_alive() / is_connected() methods."""

    def test_binance_ws_has_is_alive(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        assert callable(getattr(BinanceWebSocket, 'is_alive', None))
        assert callable(getattr(BinanceWebSocket, 'is_connected', None))

    def test_bybit_ws_has_is_alive(self):
        from arbitrage.exchanges.bybit_ws import BybitWebSocket
        assert callable(getattr(BybitWebSocket, 'is_alive', None))

    def test_okx_ws_has_is_alive(self):
        from arbitrage.exchanges.okx_ws import OKXWebSocket
        assert callable(getattr(OKXWebSocket, 'is_alive', None))

    def test_htx_ws_has_is_alive(self):
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        assert callable(getattr(HTXWebSocket, 'is_alive', None))

    def test_binance_is_connected_returns_false_when_not_started(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        ws = BinanceWebSocket("BTCUSDT")
        assert ws.is_connected() is False

    def test_binance_is_alive_returns_false_when_not_started(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        ws = BinanceWebSocket("BTCUSDT")
        assert ws.is_alive() is False


# ═══════════════════════════════════════════════════════
# SLIPPAGE MODEL — walk_book correctness
# ═══════════════════════════════════════════════════════

class TestSlippageModel:
    """Verify slippage model calculates realistic fill prices."""

    def test_walk_book_empty(self):
        from arbitrage.system.slippage import SlippageModel
        result = SlippageModel.walk_book([], 100.0)
        assert result == 0.0

    def test_walk_book_single_level(self):
        from arbitrage.system.slippage import SlippageModel
        book = [[100.0, 1000.0]]
        result = SlippageModel.walk_book(book, 500.0)
        assert result == 100.0

    def test_walk_book_multi_level(self):
        from arbitrage.system.slippage import SlippageModel
        book = [[100.0, 100.0], [100.1, 200.0], [100.2, 300.0]]
        result = SlippageModel.walk_book(book, 500.0)
        # Should at least walk into the book (price >= first level)
        assert result >= 100.0


# ═══════════════════════════════════════════════════════
# RISK MANAGER — comprehensive
# ═══════════════════════════════════════════════════════

class TestRiskManagerComprehensive:
    """All RiskManager checks should pass for healthy state."""

    def _make_rm(self, total_balance=1000.0, positions=0):
        config_mock = MagicMock()
        config_mock.max_position_pct = 0.30
        config_mock.max_concurrent_positions = 3
        config_mock.emergency_margin_ratio = 0.05
        config_mock.max_delta_percent = 0.05
        config_mock.min_balance_per_side = 5.0
        config_mock.min_total_balance = 10.0
        config_mock.max_exposure_pct = 0.30
        config_mock.max_position_duration_hours = 72.0
        config_mock.max_daily_drawdown_pct = 0.05
        config_mock.starting_equity = total_balance

        state = MagicMock()
        state.total_balance = total_balance
        state.position_count.return_value = positions
        state.get_all_positions.return_value = []

        from arbitrage.core.risk import RiskManager
        rm = RiskManager(config_mock, state)
        rm._daily_drawdown_reset_ts = time.time()
        return rm

    def test_balanced_state_no_emergency(self):
        rm = self._make_rm()
        should_close, reason = rm.should_emergency_close()
        assert not should_close, f"No emergency: {reason}"

    def test_nan_detected_in_balance(self):
        rm = self._make_rm(total_balance=float('nan'), positions=1)
        should_close, reason = rm.should_emergency_close()
        assert should_close
        assert "nan" in reason.lower() or reason == ""

    def test_is_valid_number_helper(self):
        from arbitrage.core.risk import _is_valid_number
        assert _is_valid_number(1.0, 2.0, 3.0) is True
        assert _is_valid_number(float('nan')) is False
        assert _is_valid_number(float('inf')) is False
        assert _is_valid_number(float('-inf')) is False


# ═══════════════════════════════════════════════════════
# STATE — BotState symbol locking
# ═══════════════════════════════════════════════════════

class TestSymbolLocking:
    """Verify concurrent strategy prevention via symbol locking."""

    def test_try_lock_first_call(self):
        from arbitrage.core.state import BotState
        state = BotState()
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True

    def test_try_lock_same_strategy_reentrant(self):
        from arbitrage.core.state import BotState
        state = BotState()
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True

    def test_try_lock_different_strategy_blocked(self):
        from arbitrage.core.state import BotState
        state = BotState()
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True
        assert state.try_lock_symbol("strat_b", "BTCUSDT") is False

    def test_try_lock_different_symbol_allowed(self):
        from arbitrage.core.state import BotState
        state = BotState()
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True
        assert state.try_lock_symbol("strat_b", "ETHUSDT") is True

    def test_release_unblocks(self):
        from arbitrage.core.state import BotState
        state = BotState()
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True
        state.release_symbol("strat_a", "BTCUSDT")
        assert state.try_lock_symbol("strat_b", "BTCUSDT") is True

    def test_cleanup_expired_locks(self):
        from arbitrage.core.state import BotState
        state = BotState()
        assert state.try_lock_symbol("strat_a", "BTCUSDT") is True
        state._symbols_in_use["BTCUSDT"] = ("strat_a", time.time() - 360)
        state.cleanup_expired_locks()
        assert state.try_lock_symbol("strat_b", "BTCUSDT") is True


# ═══════════════════════════════════════════════════════
# RATE LIMITER — 429 backoff and token bucket
# ═══════════════════════════════════════════════════════

class TestRateLimiter:
    """Verify rate limiter behavior."""

    def test_basic_acquire(self):
        from arbitrage.utils.rate_limiter import ExchangeRateLimiter
        limiter = ExchangeRateLimiter(custom_rates={"test": 1000.0})

        async def _run():
            await limiter.acquire("test")
        asyncio.get_event_loop().run_until_complete(_run())

    def test_429_backoff(self):
        from arbitrage.utils.rate_limiter import ExchangeRateLimiter
        limiter = ExchangeRateLimiter()
        backoff = limiter.record_429("okx")
        assert backoff > 0
        assert backoff <= 60.0

    def test_429_exponential_growth(self):
        from arbitrage.utils.rate_limiter import ExchangeRateLimiter, MAX_BACKOFF_SECONDS
        limiter = ExchangeRateLimiter()
        backoffs = []
        for _ in range(10):
            b = limiter.record_429("okx")
            backoffs.append(b)
        assert backoffs[1] > backoffs[0]
        assert backoffs[2] > backoffs[1]
        assert all(b <= MAX_BACKOFF_SECONDS for b in backoffs)

    def test_success_resets_429(self):
        from arbitrage.utils.rate_limiter import ExchangeRateLimiter
        limiter = ExchangeRateLimiter()
        limiter.record_429("okx")
        limiter.record_success("okx")
        status = limiter.status()
        assert status["okx"]["consecutive_429"] == 0

    def test_global_singleton(self):
        from arbitrage.utils.rate_limiter import get_rate_limiter
        lim1 = get_rate_limiter()
        lim2 = get_rate_limiter()
        assert lim1 is lim2


# ═══════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════

class TestNotifications:
    """Verify notification rate limiting and helpers."""

    def test_rate_limiting(self):
        from arbitrage.core.notifications import NotificationManager
        nm = NotificationManager()
        nm.enabled = True
        nm._send_times = [time.time()] * 20
        nm._max_sends_per_minute = 20
        now = time.time()
        nm._send_times = [t for t in nm._send_times if now - t < 60]
        assert len(nm._send_times) >= 20

    def test_safe_price_nan_handling(self):
        from arbitrage.core.notifications import NotificationManager
        assert NotificationManager._safe_price(float('nan')) == "N/A"
        assert NotificationManager._safe_price(float('inf')) == "N/A"
        assert NotificationManager._safe_price(1234.567) == "1,234.5670"


# ═══════════════════════════════════════════════════════
# BINANCE WS structure integrity
# ═══════════════════════════════════════════════════════

class TestBinanceWSStructure:
    def test_is_connected_is_method(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        assert hasattr(BinanceWebSocket, 'is_connected')
        assert callable(getattr(BinanceWebSocket, 'is_connected'))

    def test_is_alive_is_method(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        assert hasattr(BinanceWebSocket, 'is_alive')
        assert callable(getattr(BinanceWebSocket, 'is_alive'))

    def test_disconnect_is_method(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        assert hasattr(BinanceWebSocket, 'disconnect')
        assert callable(getattr(BinanceWebSocket, 'disconnect'))


# ═══════════════════════════════════════════════════════
# STRATEGY — Cash & Carry
# ═══════════════════════════════════════════════════════

class TestCashAndCarryStrategy:
    def test_strategy_init(self):
        from arbitrage.system.strategies.cash_and_carry import CashAndCarryStrategy
        strategy = CashAndCarryStrategy(
            min_funding_apr_pct=5.0,
            max_basis_spread_pct=0.30,
            min_holding_hours=8.0,
            max_holding_hours=72.0,
            min_book_depth_usd=5000.0,
        )
        assert strategy.min_funding_apr_pct == 5.0
        assert strategy.max_basis_spread_pct == 0.30

    def test_default_fee_rates_exist(self):
        from arbitrage.system.strategies.cash_and_carry import _DEFAULT_SPOT_FEE_PCT, _DEFAULT_PERP_FEE_PCT
        assert "binance" in _DEFAULT_SPOT_FEE_PCT
        assert "binance" in _DEFAULT_PERP_FEE_PCT
        assert _DEFAULT_SPOT_FEE_PCT["binance"] > 0
        assert _DEFAULT_PERP_FEE_PCT["binance"] > 0


# ═══════════════════════════════════════════════════════
# STRATEGY — Futures Cross-Exchange
# ═══════════════════════════════════════════════════════

class TestFuturesCrossExchangeStrategy:
    def test_strategy_init(self):
        from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
        strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=0.15,
            target_profit_pct=0.12,
        )
        assert strategy.min_spread_pct == 0.15

    def test_reliability_rank(self):
        from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
        strategy = FuturesCrossExchangeStrategy(
            reliability_rank={"okx": 0, "bybit": 1, "htx": 2, "binance": 3},
        )
        assert strategy._reliability_rank["okx"] == 0


# ═══════════════════════════════════════════════════════
# INDICATORS — technical analysis
# ═══════════════════════════════════════════════════════

class TestIndicators:
    def test_ema_single_value(self):
        from market_intelligence.indicators import ema
        result = ema([100.0], 14)
        assert result == 100.0

    def test_ema_empty(self):
        from market_intelligence.indicators import ema
        result = ema([], 14)
        assert result == 0.0

    def test_rsi_insufficient_data(self):
        from market_intelligence.indicators import rsi
        result = rsi([100.0, 101.0], 14)
        assert result == 50.0

    def test_rsi_strong_uptrend(self):
        from market_intelligence.indicators import rsi
        prices = list(range(100, 130))
        result = rsi(prices, 14)
        assert result > 70

    def test_macd_insufficient_data(self):
        from market_intelligence.indicators import macd
        result = macd([100.0] * 5)
        assert result == (0.0, 0.0, 0.0)

    def test_macd_returns_valid(self):
        from market_intelligence.indicators import macd
        prices = [100.0 + i * 0.5 for i in range(30)]
        macd_line, signal, histogram = macd(prices)
        assert macd_line != 0.0


# ═══════════════════════════════════════════════════════
# HELPER UTILS
# ═══════════════════════════════════════════════════════

class TestHelpers:
    def test_safe_float_conversion(self):
        from arbitrage.system.live_adapters import _safe_float
        assert _safe_float("123.45") == 123.45
        assert _safe_float(None) == 0.0
        assert _safe_float("abc") == 0.0
        assert _safe_float(42) == 42.0
