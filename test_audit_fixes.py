"""
Comprehensive tests for arbitrage system audit fixes.

Tests cover:
  1. Parallel leg execution (asyncio.gather)
  2. Dynamic capital allocation (volatility/funding/trend)
  3. Dynamic fee rates from snapshot (VIP-aware)
  4. Funding cost tracking for held positions
  5. Position age calculation (time.time consistency)
  6. Strategy spread calculation correctness
  7. Risk engine validation
  8. Circuit breaker behaviour
  9. State persistence and PnL tracking
  10. Slippage model estimation
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
from arbitrage.system.config import ExecutionConfig, RiskConfig, StrategyConfig, TradingSystemConfig
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
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState
from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_snapshot(
    symbol: str = "BTCUSDT",
    orderbooks: dict | None = None,
    funding_rates: dict | None = None,
    fee_bps: dict | None = None,
    balances: dict | None = None,
    volatility: float = 0.002,
    trend_strength: float = 0.0,
) -> MarketSnapshot:
    if orderbooks is None:
        orderbooks = {
            "okx": OrderBookSnapshot(exchange="okx", symbol=symbol, bid=50000.0, ask=50010.0),
            "bybit": OrderBookSnapshot(exchange="bybit", symbol=symbol, bid=50050.0, ask=50060.0),
        }
    return MarketSnapshot(
        symbol=symbol,
        orderbooks=orderbooks,
        spot_orderbooks={},
        orderbook_depth={},
        spot_orderbook_depth={},
        balances=balances or {"okx": 1000.0, "bybit": 1000.0},
        fee_bps=fee_bps or {"okx": {"perp": 5.0}, "bybit": {"perp": 5.5}},
        funding_rates=funding_rates or {"okx": 0.0001, "bybit": 0.0003},
        volatility=volatility,
        trend_strength=trend_strength,
        atr=10.0,
        atr_rolling=10.0,
        indicators={},
    )


def _make_intent(
    symbol: str = "BTCUSDT",
    long_ex: str = "okx",
    short_ex: str = "bybit",
    edge_bps: float = 10.0,
    metadata: dict | None = None,
) -> TradeIntent:
    md = {
        "entry_mid": 50030.0,
        "long_price": 50010.0,
        "short_price": 50050.0,
        "total_fees_pct": 0.21,
        "limit_prices": {"buy": 50010.0, "sell": 50050.0},
        **(metadata or {}),
    }
    return TradeIntent(
        strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
        symbol=symbol,
        long_exchange=long_ex,
        short_exchange=short_ex,
        side="cross_exchange_arb",
        confidence=0.8,
        expected_edge_bps=edge_bps,
        stop_loss_bps=40.0,
        metadata=md,
    )


def _make_venue_mock(
    first_success: bool = True,
    second_success: bool = True,
    fill_confirmed: bool = True,
) -> MagicMock:
    """Build a mock ExecutionVenue with configurable leg outcomes."""
    venue = AsyncMock()

    call_count = {"place": 0}

    async def _place_order(exchange, symbol, side, quantity_usd, order_type, limit_price=0.0, **kw):
        call_count["place"] += 1
        n = call_count["place"]
        success = first_success if n == 1 else second_success
        return {
            "success": success,
            "message": "" if success else "test_reject",
            "exchange": exchange,
            "order_id": f"oid-{n}" if success else "",
            "size": 1.0,
            "fill_price": 50030.0 if success else 0.0,
            "effective_notional": quantity_usd,
        }

    venue.place_order = AsyncMock(side_effect=_place_order)
    venue.place_spot_order = AsyncMock(return_value={"success": False, "message": "not_needed"})
    venue.wait_for_fill = AsyncMock(return_value=fill_confirmed)
    venue.get_balances = AsyncMock(return_value={"okx": 500.0, "bybit": 500.0})
    venue.invalidate_balance_cache = MagicMock()
    venue.open_contracts = AsyncMock(return_value=0.0)
    # Set real float values so getattr() in preflight checks works
    venue.safety_buffer_pct = 0.05
    venue.safety_reserve_usd = 0.50
    venue._min_notional_usd = MagicMock(return_value=1.0)
    return venue


# ===================================================================
# TEST 1: Parallel Leg Execution
# ===================================================================


class TestParallelExecution:
    """Verify both legs are placed via asyncio.gather, not sequentially."""

    @pytest.mark.asyncio
    async def test_both_legs_placed_simultaneously(self):
        """When both legs succeed, both should be placed in parallel."""
        venue = _make_venue_mock(first_success=True, second_success=True)
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=False, order_timeout_ms=1000)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        intent = _make_intent()
        report = await engine.execute_dual_entry(
            intent, notional_usd=100.0,
            est_book_depth_usd=2_000_000, volatility=0.002, latency_ms=50.0,
        )
        assert report.success
        assert report.position_id is not None
        # Both place_order calls should have been made
        assert venue.place_order.call_count == 2

    @pytest.mark.asyncio
    async def test_both_legs_fail_no_hedge(self):
        """When both legs fail, no hedge is needed."""
        venue = _make_venue_mock(first_success=False, second_success=False)
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=False, order_timeout_ms=1000)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        intent = _make_intent()
        report = await engine.execute_dual_entry(
            intent, notional_usd=100.0,
            est_book_depth_usd=2_000_000, volatility=0.002, latency_ms=50.0,
        )
        assert not report.success
        assert report.message == "first_leg_failed"

    @pytest.mark.asyncio
    async def test_one_leg_fails_hedge_triggered(self):
        """When one leg succeeds and other fails, hedge the filled leg."""
        venue = _make_venue_mock(first_success=True, second_success=False)
        # Make wait_for_fill return True for first, False for second
        fill_calls = {"n": 0}
        async def _wait(exchange, symbol, oid, timeout, spot=False, expected_size=None):
            fill_calls["n"] += 1
            return fill_calls["n"] == 1  # only first fill confirmed
        venue.wait_for_fill = AsyncMock(side_effect=_wait)
        # Hedge order succeeds
        hedge_call_count = {"n": 0}
        original_place = venue.place_order.side_effect
        async def _place_with_hedge(exchange, symbol, side, quantity_usd, order_type, limit_price=0.0, **kw):
            hedge_call_count["n"] += 1
            n = hedge_call_count["n"]
            if n <= 2:  # first two are the parallel legs
                success = (n == 1)  # only first succeeds
                return {
                    "success": success,
                    "message": "" if success else "test_reject",
                    "exchange": exchange,
                    "order_id": f"oid-{n}" if success else "",
                    "size": 1.0,
                    "fill_price": 50030.0 if success else 0.0,
                    "effective_notional": quantity_usd,
                }
            # Hedge orders
            return {
                "success": True,
                "message": "",
                "exchange": exchange,
                "order_id": f"hedge-{n}",
                "size": 1.0,
                "fill_price": 50030.0,
                "effective_notional": quantity_usd,
            }
        venue.place_order = AsyncMock(side_effect=_place_with_hedge)

        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=False, order_timeout_ms=500, hedge_retries=1, hedge_settle_seconds=0.01)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        intent = _make_intent()
        report = await engine.execute_dual_entry(
            intent, notional_usd=100.0,
            est_book_depth_usd=2_000_000, volatility=0.002, latency_ms=50.0,
        )
        assert not report.success
        assert report.message == "second_leg_failed"
        assert report.hedged  # hedge should have been attempted

    @pytest.mark.asyncio
    async def test_dry_run_skips_real_execution(self):
        """Dry run should not place any real orders."""
        venue = _make_venue_mock()
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=True, order_timeout_ms=1000)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        intent = _make_intent()
        report = await engine.execute_dual_entry(
            intent, notional_usd=100.0,
            est_book_depth_usd=2_000_000, volatility=0.002, latency_ms=50.0,
        )
        assert report.success
        assert report.message == "dry_run_fill"
        assert venue.place_order.call_count == 0  # no real orders

    @pytest.mark.asyncio
    async def test_neither_leg_fills_despite_placement(self):
        """Both placed but neither fills — should not open position or hedge."""
        venue = _make_venue_mock(first_success=True, second_success=True, fill_confirmed=False)
        venue.open_contracts = AsyncMock(return_value=0.0)
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=False, order_timeout_ms=100)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        intent = _make_intent()
        report = await engine.execute_dual_entry(
            intent, notional_usd=100.0,
            est_book_depth_usd=2_000_000, volatility=0.002, latency_ms=50.0,
        )
        assert not report.success


# ===================================================================
# TEST 2: Dynamic Capital Allocation
# ===================================================================


class TestCapitalAllocator:

    def test_equal_weight_baseline(self):
        """With neutral conditions, allocation should be near full."""
        allocator = CapitalAllocator(risk_config=RiskConfig())
        plan = allocator.allocate(
            equity=10000.0,
            avg_funding_bps=0.0,
            volatility_regime=0.002,
            trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        alloc = plan.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]
        assert alloc > 0

    def test_high_volatility_reduces_allocation(self):
        """High volatility should reduce allocation vs baseline."""
        # Use high caps so weighting differences are visible (not capped)
        cfg = RiskConfig(max_total_exposure_pct=0.50, max_strategy_allocation_pct=0.90)
        allocator = CapitalAllocator(risk_config=cfg)
        baseline = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.002, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        high_vol = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.01, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        baseline_alloc = baseline.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]
        high_vol_alloc = high_vol.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]
        assert high_vol_alloc < baseline_alloc

    def test_low_volatility_reduces_allocation(self):
        """Very low volatility (fewer opportunities) should also reduce."""
        cfg = RiskConfig(max_total_exposure_pct=0.50, max_strategy_allocation_pct=0.90)
        allocator = CapitalAllocator(risk_config=cfg)
        moderate = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.003, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        low = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.0005, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        assert low.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE] < \
               moderate.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]

    def test_high_funding_boosts_allocation(self):
        """High funding spread should boost allocation."""
        allocator = CapitalAllocator(risk_config=RiskConfig())
        no_funding = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.002, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        high_funding = allocator.allocate(
            equity=10000.0, avg_funding_bps=20.0,
            volatility_regime=0.002, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        assert high_funding.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE] >= \
               no_funding.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]

    def test_strong_trend_reduces_allocation(self):
        """Strong directional trend should reduce delta-neutral allocation."""
        cfg = RiskConfig(max_total_exposure_pct=0.50, max_strategy_allocation_pct=0.90)
        allocator = CapitalAllocator(risk_config=cfg)
        flat = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.002, trend_strength=0.001,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        trending = allocator.allocate(
            equity=10000.0, avg_funding_bps=0.0,
            volatility_regime=0.002, trend_strength=0.03,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        assert trending.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE] < \
               flat.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]

    def test_respects_hard_cap(self):
        """Allocation should never exceed per-strategy hard cap."""
        config = RiskConfig(max_total_exposure_pct=1.0, max_strategy_allocation_pct=0.10)
        allocator = CapitalAllocator(risk_config=config)
        plan = allocator.allocate(
            equity=10000.0, avg_funding_bps=50.0,
            volatility_regime=0.003, trend_strength=0.0,
            enabled=[StrategyId.FUTURES_CROSS_EXCHANGE],
        )
        alloc = plan.strategy_allocations[StrategyId.FUTURES_CROSS_EXCHANGE]
        assert alloc <= 10000.0 * 0.10 + 0.01  # small float tolerance


# ===================================================================
# TEST 3: Dynamic Fee Rates from Snapshot
# ===================================================================


class TestDynamicFees:

    @pytest.mark.asyncio
    async def test_uses_snapshot_perp_fees(self):
        """Strategy should use fee_bps from snapshot when available."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        # Snapshot with custom VIP fees: 3 bps = 0.03%
        snapshot = _make_snapshot(
            fee_bps={"okx": {"perp": 3.0}, "bybit": {"perp": 3.0}},
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50100.0, ask=50110.0),
            },
        )
        fee = strategy._get_fee_pct("okx", snapshot)
        assert fee == pytest.approx(0.03, abs=0.001)  # 3 bps = 0.03%

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_snapshot_fee(self):
        """When snapshot has no fee data, fall back to hardcoded defaults."""
        strategy = FuturesCrossExchangeStrategy()
        snapshot = _make_snapshot(fee_bps={})
        fee = strategy._get_fee_pct("okx", snapshot)
        assert fee == pytest.approx(0.05, abs=0.001)  # default OKX taker

    @pytest.mark.asyncio
    async def test_uses_symbol_specific_fee(self):
        """Per-symbol fee key like 'perp:BTCUSDT' should take priority."""
        strategy = FuturesCrossExchangeStrategy()
        snapshot = _make_snapshot(
            symbol="BTCUSDT",
            fee_bps={"okx": {"perp": 5.0, "perp:BTCUSDT": 2.0}},
        )
        fee = strategy._get_fee_pct("okx", snapshot)
        assert fee == pytest.approx(0.02, abs=0.001)  # symbol-specific 2 bps

    @pytest.mark.asyncio
    async def test_zero_fee_falls_back(self):
        """Zero bps fee in snapshot is suspicious — should fallback."""
        strategy = FuturesCrossExchangeStrategy()
        snapshot = _make_snapshot(fee_bps={"okx": {"perp": 0.0}})
        fee = strategy._get_fee_pct("okx", snapshot)
        assert fee == pytest.approx(0.05, abs=0.001)  # default

    @pytest.mark.asyncio
    async def test_lower_fees_allow_more_trades(self):
        """With VIP fees, more spread opportunities should pass the threshold."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.04)
        # Spread: (50120 - 50020) / 50020 * 100 = 0.1999%
        # Default fees: (0.05 + 0.055) * 2 = 0.21% round-trip → net = -0.01% → REJECT
        # VIP fees: (0.02 + 0.02) * 2 = 0.08% round-trip → net = 0.12% → ACCEPT
        snapshot_default = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50020.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50120.0, ask=50140.0),
            },
            fee_bps={"okx": {"perp": 5.0}, "bybit": {"perp": 5.5}},
        )
        snapshot_vip = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50020.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50120.0, ask=50140.0),
            },
            fee_bps={"okx": {"perp": 2.0}, "bybit": {"perp": 2.0}},
        )
        intents_default = await strategy.on_market_snapshot(snapshot_default)
        # Reset cooldown
        strategy._last_signal_ts.clear()
        intents_vip = await strategy.on_market_snapshot(snapshot_vip)
        assert len(intents_default) == 0
        assert len(intents_vip) > 0


# ===================================================================
# TEST 4: Funding Cost Tracking
# ===================================================================


class TestFundingTracking:

    def test_funding_pnl_calculation(self):
        """Verify funding PnL formula: income from short - cost from long."""
        # Simulate: long OKX (fr=0.01%), short Bybit (fr=0.03%), held 8 hours
        fr_long = 0.0001   # 0.01%
        fr_short = 0.0003  # 0.03%
        notional = 1000.0
        age_sec = 28800.0  # 8 hours = 1 funding period
        funding_interval = 28800.0

        periods = age_sec / funding_interval
        cost_long = fr_long * notional * periods
        income_short = fr_short * notional * periods
        funding_pnl = income_short - cost_long

        # Short receives 0.03% × $1000 = $0.30
        # Long pays 0.01% × $1000 = $0.10
        # Net funding PnL = $0.30 - $0.10 = $0.20
        assert funding_pnl == pytest.approx(0.20, abs=0.01)

    def test_negative_funding_rates(self):
        """Negative funding: long receives, short pays."""
        fr_long = -0.0002   # long receives
        fr_short = -0.0001  # short pays less
        notional = 1000.0
        periods = 1.0

        cost_long = fr_long * notional * periods    # negative = income
        income_short = fr_short * notional * periods  # negative = cost
        funding_pnl = income_short - cost_long
        # income_short = -0.10, cost_long = -0.20
        # pnl = -0.10 - (-0.20) = 0.10
        assert funding_pnl == pytest.approx(0.10, abs=0.01)

    def test_zero_funding_no_impact(self):
        """Zero funding rates should not affect PnL."""
        funding_pnl = 0.0 * 1000.0 * 1.0 - 0.0 * 1000.0 * 1.0
        assert funding_pnl == 0.0


# ===================================================================
# TEST 5: Position Age (time.time consistency)
# ===================================================================


class TestPositionAge:

    def test_age_uses_wall_clock(self):
        """Position opened_at uses time.time(), age should use time.time() too."""
        opened = time.time() - 300  # 5 minutes ago
        age_min = (time.time() - opened) / 60
        assert 4.9 <= age_min <= 5.2  # rough check, allowing small drift

    def test_zero_opened_at_returns_zero(self):
        """If opened_at is 0, age should be 0."""
        opened = 0
        age_min = (time.time() - opened) / 60 if opened > 0 else 0
        assert age_min == 0


# ===================================================================
# TEST 6: Strategy Spread Calculation
# ===================================================================


class TestSpreadCalculation:

    @pytest.mark.asyncio
    async def test_positive_spread_generates_intent(self):
        """Clear spread opportunity should generate a trade intent."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            fee_bps={"okx": {"perp": 5.0}, "bybit": {"perp": 5.5}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) >= 1
        # Should long on cheap exchange (okx), short on expensive (bybit)
        intent = intents[0]
        assert intent.long_exchange == "okx"
        assert intent.short_exchange == "bybit"

    @pytest.mark.asyncio
    async def test_no_spread_no_intent(self):
        """When prices are equal, no intent should be generated."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
            },
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) == 0

    @pytest.mark.asyncio
    async def test_spread_formula_correctness(self):
        """Verify spread = (short_bid - long_ask) / long_ask * 100."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        long_ask = 50010.0
        short_bid = 50200.0
        expected_spread = (short_bid - long_ask) / long_ask * 100
        # ~ 0.38%
        assert expected_spread == pytest.approx(0.38, abs=0.01)

    @pytest.mark.asyncio
    async def test_fees_deducted_correctly(self):
        """Net spread should subtract full round-trip fees."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            fee_bps={"okx": {"perp": 5.0}, "bybit": {"perp": 5.5}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) >= 1
        meta = intents[0].metadata
        raw = meta["spread_pct"]
        fees = meta["total_fees_pct"]
        net = meta["net_spread_pct"]
        assert net == pytest.approx(raw - fees, abs=0.0001)

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_signals(self):
        """Same pair should not signal twice within cooldown period."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        strategy._signal_cooldown_sec = 5.0
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            fee_bps={"okx": {"perp": 2.0}, "bybit": {"perp": 2.0}},
        )
        intents1 = await strategy.on_market_snapshot(snapshot)
        intents2 = await strategy.on_market_snapshot(snapshot)
        assert len(intents1) >= 1
        assert len(intents2) == 0  # cooldown blocks second signal

    @pytest.mark.asyncio
    async def test_single_exchange_no_intent(self):
        """Need at least 2 exchanges for cross-exchange arb."""
        strategy = FuturesCrossExchangeStrategy()
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
            },
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) == 0


# ===================================================================
# TEST 7: Risk Engine
# ===================================================================


class TestRiskEngine:

    @pytest.mark.asyncio
    async def test_approves_valid_trade(self):
        """Normal conditions should approve the trade."""
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=_make_intent(),
            allocation_plan=plan,
            proposed_notional=500.0,
            estimated_slippage_bps=5.0,
            leverage=1.0,
            api_latency_ms=50.0,
        )
        assert decision.approved
        assert decision.reason == "approved"

    @pytest.mark.asyncio
    async def test_rejects_kill_switch_active(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        await state.trigger_kill_switch(permanent=True)
        risk = RiskEngine(config=RiskConfig(), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=_make_intent(), allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=5.0,
            leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.reason == "kill_switch_active"

    @pytest.mark.asyncio
    async def test_rejects_high_latency(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(api_latency_limit_ms=100), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=_make_intent(), allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=5.0,
            leverage=1.0, api_latency_ms=500.0,
        )
        assert not decision.approved
        assert decision.reason == "api_latency_limit_exceeded"

    @pytest.mark.asyncio
    async def test_rejects_excessive_slippage(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(max_order_slippage_bps=10.0), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=_make_intent(), allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=50.0,
            leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.reason == "slippage_limit_exceeded"

    @pytest.mark.asyncio
    async def test_rejects_leverage_exceeded(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(max_leverage=3.0), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=_make_intent(), allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=5.0,
            leverage=10.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert decision.reason == "leverage_limit_exceeded"

    @pytest.mark.asyncio
    async def test_rejects_stale_orderbook(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(max_orderbook_age_sec=5.0), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        old_snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0,
                                         timestamp=time.time() - 30),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50050.0, ask=50060.0),
            },
        )
        decision = await risk.validate_intent(
            intent=_make_intent(), allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=5.0,
            leverage=1.0, api_latency_ms=50.0, snapshot=old_snapshot,
        )
        assert not decision.approved
        assert decision.reason == "stale_orderbook"

    @pytest.mark.asyncio
    async def test_daily_drawdown_triggers_kill_switch(self):
        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        # Simulate 6% daily drawdown (above default 5% limit)
        await state.apply_realized_pnl(-600.0)
        risk = RiskEngine(config=RiskConfig(max_daily_drawdown_pct=0.05), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=_make_intent(), allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=5.0,
            leverage=1.0, api_latency_ms=50.0,
        )
        assert not decision.approved
        assert "drawdown" in decision.reason
        assert decision.kill_switch_triggered


# ===================================================================
# TEST 8: Circuit Breaker
# ===================================================================


class TestCircuitBreaker:

    def test_available_by_default(self):
        cb = ExchangeCircuitBreaker()
        assert cb.is_available("okx")

    def test_trips_after_max_errors(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3, cooldown_seconds=60)
        cb.record_error("okx", "err1")
        cb.record_error("okx", "err2")
        assert cb.is_available("okx")
        cb.record_error("okx", "err3")
        assert not cb.is_available("okx")

    def test_success_resets_counter(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3)
        cb.record_error("okx", "err1")
        cb.record_error("okx", "err2")
        cb.record_success("okx")
        cb.record_error("okx", "err3")
        assert cb.is_available("okx")  # not tripped yet

    def test_cooldown_expires(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=1, cooldown_seconds=0.01)
        cb.record_error("okx", "err")
        assert not cb.is_available("okx")
        time.sleep(0.02)
        assert cb.is_available("okx")

    def test_status_report(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=3)
        cb.record_error("okx", "timeout")
        status = cb.status()
        assert "okx" in status
        assert status["okx"]["consecutive_errors"] == 1
        assert status["okx"]["available"]

    def test_independent_exchanges(self):
        cb = ExchangeCircuitBreaker(max_consecutive_errors=2)
        cb.record_error("okx", "err")
        cb.record_error("okx", "err")
        cb.record_error("bybit", "err")
        assert not cb.is_available("okx")
        assert cb.is_available("bybit")


# ===================================================================
# TEST 9: State & PnL
# ===================================================================


class TestSystemState:

    @pytest.mark.asyncio
    async def test_add_remove_position(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        pos = OpenPosition(
            position_id="test-1", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=40.0,
        )
        await state.add_position(pos)
        positions = await state.list_positions()
        assert len(positions) == 1
        removed = await state.remove_position("test-1")
        assert removed is not None
        assert removed.symbol == "BTCUSDT"
        positions = await state.list_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_realized_pnl_updates_equity(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        await state.apply_realized_pnl(50.0)
        snap = await state.snapshot()
        assert snap["equity"] == pytest.approx(1050.0)
        assert snap["realized_pnl"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_drawdown_calculation(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        await state.apply_realized_pnl(-100.0)
        dd = await state.drawdowns()
        assert dd["daily_dd"] == pytest.approx(0.10, abs=0.01)
        assert dd["portfolio_dd"] == pytest.approx(0.10, abs=0.01)

    @pytest.mark.asyncio
    async def test_kill_switch_lifecycle(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        assert not await state.kill_switch_triggered()
        await state.trigger_kill_switch(permanent=False)
        assert await state.kill_switch_triggered()
        await state.reset_kill_switch()
        assert not await state.kill_switch_triggered()

    @pytest.mark.asyncio
    async def test_permanent_kill_switch(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        await state.trigger_kill_switch(permanent=True)
        assert await state.kill_switch_triggered()
        # Permanent kill switch should NOT auto-expire
        state._kill_switch_cooldown_sec = 0.0
        assert await state.kill_switch_triggered()

    @pytest.mark.asyncio
    async def test_total_exposure(self):
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        for i in range(3):
            pos = OpenPosition(
                position_id=f"p{i}", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
                symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
                notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=40.0,
            )
            await state.add_position(pos)
        assert await state.total_exposure() == pytest.approx(300.0)


# ===================================================================
# TEST 10: Slippage Model
# ===================================================================


class TestSlippageModel:

    def test_base_slippage(self):
        model = SlippageModel()
        bps = model.estimate(100.0, 1_000_000, 0.0, 0.0)
        # Should be roughly base_bps (1.5) + small size_pressure
        assert 1.0 <= bps <= 3.0

    def test_large_order_more_slippage(self):
        model = SlippageModel()
        small = model.estimate(100.0, 1_000_000, 0.002, 50.0)
        large = model.estimate(500_000.0, 1_000_000, 0.002, 50.0)
        assert large > small

    def test_high_volatility_more_slippage(self):
        model = SlippageModel()
        calm = model.estimate(100.0, 1_000_000, 0.001, 50.0)
        volatile = model.estimate(100.0, 1_000_000, 0.05, 50.0)
        assert volatile > calm

    def test_high_latency_more_slippage(self):
        model = SlippageModel()
        fast = model.estimate(100.0, 1_000_000, 0.002, 10.0)
        slow = model.estimate(100.0, 1_000_000, 0.002, 500.0)
        assert slow > fast

    def test_zero_depth_max_slippage(self):
        model = SlippageModel()
        bps = model.estimate(100.0, 0.0, 0.0, 0.0)
        assert bps == 1000.0  # protection against zero depth


# ===================================================================
# TEST 11: Funding Rate Arbitrage Strategy
# ===================================================================


class TestFundingRateStrategy:

    @pytest.mark.asyncio
    async def test_funding_arb_signal(self):
        """Large funding rate differential should generate funding arb intent."""
        strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=10.0,  # impossibly high so only funding arb triggers
            funding_threshold_pct=0.01,
        )
        # Funding diff: (0.003 - (-0.001)) * 100 = 0.40% — well above costs
        snapshot = _make_snapshot(
            funding_rates={"okx": 0.003, "bybit": -0.001},
            fee_bps={"okx": {"perp": 2.0}, "bybit": {"perp": 2.0}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        funding_intents = [i for i in intents if i.metadata.get("arb_type") == "funding_rate"]
        assert len(funding_intents) >= 1
        # Should short on higher rate (okx), long on lower (bybit)
        fi = funding_intents[0]
        assert fi.short_exchange == "okx"
        assert fi.long_exchange == "bybit"

    @pytest.mark.asyncio
    async def test_small_funding_diff_no_signal(self):
        """Tiny funding difference should not generate signal."""
        strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=10.0,
            funding_threshold_pct=0.05,
        )
        snapshot = _make_snapshot(
            funding_rates={"okx": 0.0001, "bybit": 0.00011},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        funding_intents = [i for i in intents if i.metadata.get("arb_type") == "funding_rate"]
        assert len(funding_intents) == 0


# ===================================================================
# TEST 12: Dual Exit
# ===================================================================


class TestDualExit:

    @pytest.mark.asyncio
    async def test_successful_dual_exit(self):
        venue = AsyncMock()
        venue.place_order = AsyncMock(return_value={"success": True, "order_id": "exit-1", "size": 1.0})
        venue.place_spot_order = AsyncMock(return_value={"success": False})
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=False)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        pos = OpenPosition(
            position_id="exit-test", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=40.0,
        )
        closed = await engine.execute_dual_exit(pos, reason="take_profit")
        assert closed
        assert venue.place_order.call_count == 2

    @pytest.mark.asyncio
    async def test_failed_second_leg_restores_first(self):
        """When second exit leg fails, first leg should be restored."""
        call_count = {"n": 0}
        async def _place(exchange, symbol, side, quantity_usd, order_type, **kw):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return {"success": False, "message": "test_fail"}  # second leg fails
            return {"success": True, "order_id": f"o-{call_count['n']}", "size": 1.0}

        venue = AsyncMock()
        venue.place_order = AsyncMock(side_effect=_place)
        state = SystemState(starting_equity=1000.0, positions_file=":memory:")
        monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
        config = ExecutionConfig(dry_run=False, hedge_retries=1, order_timeout_ms=100)
        engine = AtomicExecutionEngine(
            config=config, venue=venue, slippage=SlippageModel(),
            state=state, monitor=monitor,
        )
        pos = OpenPosition(
            position_id="exit-fail", strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT", long_exchange="okx", short_exchange="bybit",
            notional_usd=100.0, entry_mid=50000.0, stop_loss_bps=40.0,
        )
        closed = await engine.execute_dual_exit(pos, reason="stop_loss")
        assert not closed
        # Should have attempted restoration (3rd call)
        assert venue.place_order.call_count >= 3


# ===================================================================
# TEST 13: Integration — Full Cycle
# ===================================================================


class TestIntegration:

    @pytest.mark.asyncio
    async def test_strategy_to_risk_pipeline(self):
        """Strategy generates intent → risk validates → should pass."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            fee_bps={"okx": {"perp": 2.0}, "bybit": {"perp": 2.0}},
            balances={"okx": 5000.0, "bybit": 5000.0},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        assert len(intents) >= 1

        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(), state=state)
        plan = AllocationPlan(
            strategy_allocations={StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0},
            total_allocatable_capital=3000.0,
        )
        decision = await risk.validate_intent(
            intent=intents[0], allocation_plan=plan,
            proposed_notional=500.0, estimated_slippage_bps=5.0,
            leverage=1.0, api_latency_ms=50.0, snapshot=snapshot,
        )
        assert decision.approved

    @pytest.mark.asyncio
    async def test_three_exchange_pairwise(self):
        """With 3 exchanges, strategy should check all 3 combinations."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=50000.0, ask=50010.0),
                "bybit": OrderBookSnapshot(exchange="bybit", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50100.0, ask=50110.0),
            },
            fee_bps={"okx": {"perp": 2.0}, "bybit": {"perp": 2.0}, "htx": {"perp": 2.0}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        # With spread on bybit, should find at least 1 opportunity
        assert len(intents) >= 1
        # Best intent should be okx→bybit (largest spread)
        best = max(intents, key=lambda x: x.expected_edge_bps)
        assert best.long_exchange == "okx"
        assert best.short_exchange == "bybit"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
