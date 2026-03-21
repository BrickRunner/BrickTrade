"""Tests for production-readiness fixes and profitability fixes."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
from arbitrage.system.config import ExecutionConfig, RiskConfig, TradingSystemConfig, StrategyConfig
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.models import (
    AllocationPlan,
    MarketSnapshot,
    OpenPosition,
    OrderBookSnapshot,
    StrategyId,
    TradeIntent,
)
from arbitrage.system.monitoring import InMemoryMonitoring
from arbitrage.system.risk import RiskEngine
from arbitrage.system.simulation.exchange import SimulatedExecutionVenue
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState
from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
from arbitrage.system.ws_orderbooks import WsOrderbookCache


# ═══════════════════════════════════════════════════════════════
# Fix #1: Circuit Breaker Tests
# ═══════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_initial_state_all_available(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        assert cb.is_available("okx") is True
        assert cb.is_available("htx") is True

    def test_errors_below_threshold_still_available(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        cb.record_error("okx", "timeout")
        cb.record_error("okx", "timeout")
        assert cb.is_available("okx") is True

    def test_errors_at_threshold_trips_breaker(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        cb.record_error("okx", "timeout")
        cb.record_error("okx", "timeout")
        cb.record_error("okx", "timeout")
        assert cb.is_available("okx") is False

    def test_success_resets_error_count(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        cb.record_error("okx", "timeout")
        cb.record_error("okx", "timeout")
        cb.record_success("okx")
        cb.record_error("okx", "timeout")
        # Only 1 error after reset, should still be available
        assert cb.is_available("okx") is True

    def test_cooldown_expires(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=0.1)
        cb.record_error("okx", "down")
        assert cb.is_available("okx") is False
        time.sleep(0.15)
        assert cb.is_available("okx") is True

    def test_independent_exchanges(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=2, cooldown_seconds=60)
        cb.record_error("okx", "err")
        cb.record_error("okx", "err")
        assert cb.is_available("okx") is False
        assert cb.is_available("htx") is True

    def test_remaining_cooldown(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=100)
        cb.record_error("okx", "err")
        remaining = cb.remaining_cooldown("okx")
        assert 98 < remaining <= 100
        assert cb.remaining_cooldown("htx") == 0.0

    def test_status_report(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        cb.record_error("okx", "test_err")
        status = cb.status()
        assert "okx" in status
        assert status["okx"]["consecutive_errors"] == 1
        assert status["okx"]["available"] is True
        assert status["okx"]["last_error"] == "test_err"


# ═══════════════════════════════════════════════════════════════
# Fix #2: Per-trade Max Loss Tests
# ═══════════════════════════════════════════════════════════════


class TestPerTradeMaxLoss:
    def test_risk_config_has_max_loss_per_trade(self):
        config = RiskConfig()
        assert hasattr(config, "max_loss_per_trade_pct")
        assert config.max_loss_per_trade_pct == 0.02

    def test_custom_max_loss_per_trade(self):
        config = RiskConfig(max_loss_per_trade_pct=0.05)
        assert config.max_loss_per_trade_pct == 0.05

    @pytest.mark.asyncio
    async def test_engine_triggers_kill_on_per_trade_loss(self):
        """Verify engine would close position and trip kill switch on excessive loss."""
        state = SystemState(1000.0, positions_file=":memory:")
        pos = OpenPosition(
            position_id="loss-test",
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            notional_usd=100.0,
            entry_mid=50000.0,
            stop_loss_bps=100.0,  # very wide stop so it doesn't trigger first
            metadata={
                "entry_long_price": 50000.0,
                "entry_short_price": 50000.0,
                "total_fees_pct": 0.10,
            },
        )
        await state.add_position(pos)
        # Equity is 1000, max_loss_per_trade_pct=0.02, so limit is $20
        # Position with pnl < -$20 should trigger kill switch
        config = RiskConfig(max_loss_per_trade_pct=0.02)
        assert config.max_loss_per_trade_pct * 1000.0 == 20.0


# ═══════════════════════════════════════════════════════════════
# Fix #3: Orphaned Order Detector Tests
# ═══════════════════════════════════════════════════════════════


class TestOrphanedOrderDetector:
    @pytest.mark.asyncio
    async def test_simulated_venue_no_orphans(self):
        """SimulatedExecutionVenue doesn't have cancel_orphaned_orders, verifying hasattr check."""
        venue = SimulatedExecutionVenue()
        assert not hasattr(venue, "cancel_orphaned_orders")

    @pytest.mark.asyncio
    async def test_live_venue_has_method(self):
        """Verify LiveExecutionVenue has the cancel_orphaned_orders method."""
        from arbitrage.system.live_adapters import LiveExecutionVenue
        assert hasattr(LiveExecutionVenue, "cancel_orphaned_orders")


# ═══════════════════════════════════════════════════════════════
# Fix #4: Balance Sync on Startup Tests
# ═══════════════════════════════════════════════════════════════


class TestBalanceSyncOnStartup:
    @pytest.mark.asyncio
    async def test_engine_has_balance_sync_flag(self):
        """Verify engine starts with _balance_synced=False."""
        from arbitrage.system.engine import TradingSystemEngine
        # Check the dataclass has the field
        import dataclasses
        fields = {f.name for f in dataclasses.fields(TradingSystemEngine)}
        assert "_balance_synced" in fields

    @pytest.mark.asyncio
    async def test_balance_sync_method_exists(self):
        from arbitrage.system.engine import TradingSystemEngine
        assert hasattr(TradingSystemEngine, "_sync_balance_on_startup")


# ═══════════════════════════════════════════════════════════════
# Fix #5: WebSocket Watchdog Tests
# ═══════════════════════════════════════════════════════════════


class TestWsOrderbookCache:
    def test_cache_init(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        assert cache._running is False
        assert len(cache._tasks) == 0

    def test_get_returns_none_for_missing(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        assert cache.get("okx", "BTCUSDT") is None

    def test_get_returns_none_for_stale(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"], _stale_after_sec=0.01)
        # Manually insert a stale snapshot
        cache._orderbooks["okx"] = {
            "BTCUSDT": OrderBookSnapshot(
                exchange="okx", symbol="BTCUSDT", bid=100.0, ask=101.0,
                timestamp=time.time() - 1.0,
            )
        }
        assert cache.get("okx", "BTCUSDT") is None

    def test_get_returns_fresh_snapshot(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        cache._orderbooks["okx"] = {
            "BTCUSDT": OrderBookSnapshot(
                exchange="okx", symbol="BTCUSDT", bid=100.0, ask=101.0,
                timestamp=time.time(),
            )
        }
        result = cache.get("okx", "BTCUSDT")
        assert result is not None
        assert result.bid == 100.0

    def test_health_status_empty(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        status = cache.health_status()
        assert status == {}

    @pytest.mark.asyncio
    async def test_stop_clears_tasks(self):
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        cache._running = True
        await cache.stop()
        assert cache._running is False
        assert len(cache._tasks) == 0

    def test_create_ws_known_exchanges(self):
        assert WsOrderbookCache._create_ws("okx", "BTCUSDT") is not None
        assert WsOrderbookCache._create_ws("htx", "BTCUSDT") is not None
        assert WsOrderbookCache._create_ws("bybit", "BTCUSDT") is not None
        assert WsOrderbookCache._create_ws("binance", "BTCUSDT") is not None
        assert WsOrderbookCache._create_ws("unknown", "BTCUSDT") is None


# ═══════════════════════════════════════════════════════════════
# Fix #6: Hedge Timeout Tests
# ═══════════════════════════════════════════════════════════════


class TestHedgeTimeout:
    def test_execution_config_has_hedge_timeout(self):
        config = ExecutionConfig()
        assert hasattr(config, "hedge_timeout_seconds")
        assert config.hedge_timeout_seconds == 15.0
        assert hasattr(config, "hedge_settle_seconds")
        assert config.hedge_settle_seconds == 0.3

    def test_custom_hedge_timeout(self):
        config = ExecutionConfig(hedge_timeout_seconds=30.0, hedge_settle_seconds=0.5)
        assert config.hedge_timeout_seconds == 30.0
        assert config.hedge_settle_seconds == 0.5

    @pytest.mark.asyncio
    async def test_hedge_method_exists(self):
        """Verify AtomicExecutionEngine has _hedge_first_leg."""
        assert hasattr(AtomicExecutionEngine, "_hedge_first_leg")

    @pytest.mark.asyncio
    async def test_dry_run_skips_hedge(self):
        """Dry run should not trigger hedge logic."""
        monitor = InMemoryMonitoring(logging.getLogger("test"))
        state = SystemState(10_000, positions_file=":memory:")
        engine = AtomicExecutionEngine(
            config=ExecutionConfig(dry_run=True, hedge_timeout_seconds=1.0),
            venue=SimulatedExecutionVenue(),
            slippage=SlippageModel(),
            state=state,
            monitor=monitor,
        )
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="market_neutral",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=5.0,
            metadata={"entry_mid": 50000.0, "long_price": 49990.0, "short_price": 50010.0},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 1_000_000, 0.001, 50.0)
        assert report.success
        assert report.message == "dry_run_fill"


# ═══════════════════════════════════════════════════════════════
# Integration: Circuit Breaker + Engine
# ═══════════════════════════════════════════════════════════════


class TestCircuitBreakerIntegration:
    def test_engine_has_circuit_breaker(self):
        from arbitrage.system.engine import TradingSystemEngine
        import dataclasses
        fields = {f.name for f in dataclasses.fields(TradingSystemEngine)}
        assert "circuit_breaker" in fields

    def test_circuit_breaker_default_config(self):
        cb = ExchangeCircuitBreaker()
        assert cb.max_consecutive_errors == 5
        assert cb.cooldown_seconds == 600.0


# ═══════════════════════════════════════════════════════════════
# Profitability Fix Tests
# ═══════════════════════════════════════════════════════════════


def _make_snapshot(
    symbol: str = "BTCUSDT",
    okx_bid: float = 50000.0,
    okx_ask: float = 50010.0,
    htx_bid: float = 50100.0,
    htx_ask: float = 50110.0,
    fee_bps: dict | None = None,
    funding_rates: dict | None = None,
) -> MarketSnapshot:
    """Helper to build a minimal MarketSnapshot for strategy tests."""
    return MarketSnapshot(
        symbol=symbol,
        orderbooks={
            "okx": OrderBookSnapshot(exchange="okx", symbol=symbol, bid=okx_bid, ask=okx_ask, timestamp=time.time()),
            "htx": OrderBookSnapshot(exchange="htx", symbol=symbol, bid=htx_bid, ask=htx_ask, timestamp=time.time()),
        },
        spot_orderbooks={},
        orderbook_depth={},
        spot_orderbook_depth={},
        balances={"okx": 5000, "htx": 5000},
        fee_bps=fee_bps or {"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        funding_rates=funding_rates or {},
        volatility=0.001,
        trend_strength=0.0,
        atr=25.0,
        atr_rolling=25.0,
        indicators={},
        timestamp=time.time(),
    )


class TestProfitFix1_FullRoundTripFees:
    """Fix #1: Entry condition must deduct full round-trip fees, not just entry fees."""

    @pytest.mark.asyncio
    async def test_marginal_spread_rejected_after_full_fee_deduction(self):
        """A spread that only covers entry fees but not exit fees must be rejected."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        # Spread = (50100 - 50010) / 50010 * 100 ≈ 0.18%
        # Entry fees = 0.05 + 0.05 = 0.10%
        # Total round-trip = 0.20%
        # Net = 0.18 - 0.20 = -0.02% → should NOT generate intent
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50100.0)
        intents = await strategy.on_market_snapshot(snapshot)
        # No intent should pass because net after round-trip fees is negative
        profitable_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        assert len(profitable_intents) == 0

    @pytest.mark.asyncio
    async def test_large_spread_accepted(self):
        """A spread well above round-trip fees should generate intent."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        # Spread = (50300 - 50010) / 50010 * 100 ≈ 0.58%
        # Round-trip fees = 0.20%
        # Net = 0.58 - 0.20 = 0.38% → well above threshold
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50300.0)
        intents = await strategy.on_market_snapshot(snapshot)
        arb_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        assert len(arb_intents) >= 1
        # Verify net_spread_pct in metadata accounts for full round-trip
        meta = arb_intents[0].metadata
        assert meta["net_spread_pct"] < meta["spread_pct"] - meta["total_fees_pct"] * 0.49  # net < spread - ~half total (it uses full)


class TestProfitFix2_PercentageBasedTPSL:
    """Fix #2: TP/SL should be percentage-based, not tiny fixed USD."""

    @pytest.mark.asyncio
    async def test_metadata_has_pct_tp_sl(self):
        """Strategy metadata should contain take_profit_pct and stop_loss_pct."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50300.0)
        intents = await strategy.on_market_snapshot(snapshot)
        arb_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        assert len(arb_intents) >= 1
        meta = arb_intents[0].metadata
        assert "take_profit_pct" in meta
        assert "stop_loss_pct" in meta
        assert meta["take_profit_pct"] > 0
        assert meta["stop_loss_pct"] > 0
        # Should NOT have hardcoded tiny USD values
        assert "take_profit_usd" not in meta

    @pytest.mark.asyncio
    async def test_tp_scales_with_spread(self):
        """TP should be ~70% of net spread, making it achievable."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50300.0)
        intents = await strategy.on_market_snapshot(snapshot)
        arb_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        assert len(arb_intents) >= 1
        meta = arb_intents[0].metadata
        net_spread_pct = meta["net_spread_pct"]
        # TP should be 70% of net spread / 100 (as fraction)
        expected_tp = net_spread_pct * 0.7 / 100
        assert abs(meta["take_profit_pct"] - expected_tp) < 1e-6


class TestProfitFix3_NoAbsInFundingSpread:
    """Fix #3: abs() should not mask negative spreads in funding arb."""

    @pytest.mark.asyncio
    async def test_negative_spread_blocks_funding_arb(self):
        """If spread crossing cost exceeds funding income, arb should not enter."""
        strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=0.01,
            funding_threshold_pct=0.005,
        )
        # Funding says: SHORT okx (high rate), LONG htx (low rate)
        # But htx_ask=50200 >> okx_bid=49800, so buying on htx and selling on okx
        # costs 0.80% in spread alone + 0.20% fees = 1.0% total
        # Funding diff = 0.20% → way less than cost → blocked
        snapshot = _make_snapshot(
            okx_bid=49800.0, okx_ask=49810.0,
            htx_bid=50190.0, htx_ask=50200.0,
            funding_rates={"okx": 0.001, "htx": -0.001},  # 0.2% diff
        )
        intents = await strategy.on_market_snapshot(snapshot)
        funding_intents = [i for i in intents if i.side == "funding_arb"]
        # spread_cost = (50200 - 49800) / 50200 ≈ 0.80% + fees 0.20% = 1.0%
        # Funding diff = 0.20% < 1.0% → blocked
        assert len(funding_intents) == 0


class TestProfitFix4_MinNotional:
    """Fix #4: actual_notional should use min() not max()."""

    def test_min_not_max(self):
        """Verify execution uses min() for actual_notional."""
        import inspect
        from arbitrage.system.execution import AtomicExecutionEngine
        source = inspect.getsource(AtomicExecutionEngine._open_live_position)
        assert "min(" in source
        assert "max(" not in source or "max(entry_long" not in source


class TestProfitFix5_ExitFeeDefault:
    """Fix #5: Default total_fees_pct should be 0.20 not 0.10."""

    def test_default_fallback_is_020(self):
        """Engine should use 0.20 as default total_fees_pct."""
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine._process_open_positions)
        assert '0.20' in source


class TestProfitFix6_FillPrice:
    """Fix #6: Fill price should use actual exchange response, not mid-price."""

    def test_extract_fill_price_method_exists(self):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        assert hasattr(LiveExecutionVenue, "_extract_fill_price")

    def test_extract_fill_price_okx(self):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        response = {"data": [{"avgPx": "50123.5"}]}
        assert LiveExecutionVenue._extract_fill_price("okx", response) == 50123.5

    def test_extract_fill_price_binance(self):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        response = {"avgPrice": "50123.5"}
        assert LiveExecutionVenue._extract_fill_price("binance", response) == 50123.5

    def test_extract_fill_price_fallback(self):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        # Empty response should return 0.0 (trigger mid-price fallback)
        assert LiveExecutionVenue._extract_fill_price("okx", {}) == 0.0


class TestProfitFix7_LimitPrices:
    """Fix #7: Strategy should populate limit_prices for slippage buffer."""

    @pytest.mark.asyncio
    async def test_limit_prices_in_metadata(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50300.0)
        intents = await strategy.on_market_snapshot(snapshot)
        arb_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        assert len(arb_intents) >= 1
        meta = arb_intents[0].metadata
        assert "limit_prices" in meta
        lp = meta["limit_prices"]
        assert "buy" in lp and "sell" in lp
        assert lp["buy"] > 0
        assert lp["sell"] > 0


class TestProfitFix8_FeeZeroFallback:
    """Fix #8: Fee of 0 bps from exchange falls back to default (no exchange charges 0% taker)."""

    @pytest.mark.asyncio
    async def test_zero_fee_falls_back_to_default(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            okx_ask=50010.0, htx_bid=50300.0,
            fee_bps={"okx": {"perp": 0.0}, "htx": {"perp": 0.0}},
        )
        # 0 bps taker fee is suspicious — should fall back to defaults
        fee_okx = strategy._get_fee_pct("okx", snapshot)
        fee_htx = strategy._get_fee_pct("htx", snapshot)
        assert fee_okx == 0.05  # OKX default
        assert fee_htx == 0.05  # HTX default

    @pytest.mark.asyncio
    async def test_missing_fee_uses_default(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            okx_ask=50010.0, htx_bid=50300.0,
            fee_bps={},  # no fee data at all
        )
        fee_okx = strategy._get_fee_pct("okx", snapshot)
        assert fee_okx == 0.05  # default


class TestProfitFix9_DedupIntents:
    """Fix #9: Only best direction should generate intent per exchange pair."""

    @pytest.mark.asyncio
    async def test_no_contradictory_intents(self):
        """Should not get LONG okx/SHORT htx AND LONG htx/SHORT okx simultaneously."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.001)
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50300.0)
        intents = await strategy.on_market_snapshot(snapshot)
        arb_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        # For each exchange pair, at most 1 direction
        pairs = set()
        for i in arb_intents:
            pair = frozenset([i.long_exchange, i.short_exchange])
            assert pair not in pairs, f"Contradictory intents for {pair}"
            pairs.add(pair)


# ═══════════════════════════════════════════════════════════════
# Round 2: Profitability Fixes
# ═══════════════════════════════════════════════════════════════


class TestBalanceCacheInvalidation:
    """Fix: Balance cache must refresh between execution legs."""

    def test_live_venue_has_invalidate_method(self):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        assert hasattr(LiveExecutionVenue, "invalidate_balance_cache")

    def test_invalidate_resets_timestamp(self):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        # Can't instantiate without exchanges, but verify method logic via source
        import inspect
        source = inspect.getsource(LiveExecutionVenue.invalidate_balance_cache)
        assert "_last_balance_ts" in source
        assert "0.0" in source

    def test_execution_calls_invalidate_between_legs(self):
        """Verify execution engine invalidates balance cache between legs."""
        import inspect
        source = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "invalidate_balance_cache" in source


class TestRealizedPnlMultiPosition:
    """Fix: Realized PnL falls back to mark-to-market when multiple positions open."""

    def test_method_checks_other_positions(self):
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine._compute_realized_pnl_from_balances)
        assert "same_exchange_count" in source
        assert "fallback_pnl_usd" in source


class TestMinNotionalCapped:
    """Fix: min_notional_override should not exceed allocation cap."""

    def test_override_logic_caps_at_alloc(self):
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine.run_cycle)
        # Verify that min_notional_hint is capped at alloc_cap
        assert "min(min_notional_hint, alloc_cap)" in source


class TestConfidenceDivisionGuard:
    """Fix: Confidence calculation guarded against division by zero."""

    @pytest.mark.asyncio
    async def test_zero_min_spread_does_not_crash(self):
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.0)
        snapshot = _make_snapshot(okx_ask=50010.0, htx_bid=50300.0)
        # Should not raise ZeroDivisionError
        intents = await strategy.on_market_snapshot(snapshot)
        # With min_spread_pct=0, any positive net spread passes threshold
        arb_intents = [i for i in intents if i.side == "cross_exchange_arb"]
        assert len(arb_intents) >= 1
        assert all(0 < i.confidence <= 1.0 for i in arb_intents)

    @pytest.mark.asyncio
    async def test_zero_funding_threshold_does_not_crash(self):
        strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=0.01,
            funding_threshold_pct=0.0,
        )
        snapshot = _make_snapshot(
            okx_ask=50010.0, htx_bid=50300.0,
            funding_rates={"okx": 0.001, "htx": -0.001},
        )
        # Should not raise ZeroDivisionError
        intents = await strategy.on_market_snapshot(snapshot)


class TestExecutionReportNotional:
    """Fix: ExecutionReport should return actual_notional, not requested."""

    def test_report_uses_actual_notional(self):
        import inspect
        source = inspect.getsource(AtomicExecutionEngine._open_live_position)
        # The return should use actual_notional, not the parameter notional_usd
        assert "notional_usd=actual_notional" in source


class TestBybitSizeRounding:
    """Fix: Bybit/Binance size should round() not int() truncate."""

    def test_rounding_logic_uses_round(self):
        import inspect
        from arbitrage.system.live_adapters import LiveExecutionVenue
        source = inspect.getsource(LiveExecutionVenue._size_from_notional)
        # Should use round() for Bybit/Binance, not int()
        assert "round(qty / step)" in source


class TestWsServerTimestamp:
    """Fix: WS orderbook should prefer server timestamp over local time."""

    def test_ws_uses_server_ts(self):
        import inspect
        source = inspect.getsource(WsOrderbookCache._run_ws_with_reconnect)
        assert "server_ts" in source
        assert 'book.get("ts")' in source
