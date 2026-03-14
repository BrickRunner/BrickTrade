from __future__ import annotations

import logging

import pytest

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.config import ExecutionConfig, RiskConfig
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.models import AllocationPlan, StrategyId, TradeIntent
from arbitrage.system.monitoring import InMemoryMonitoring
from arbitrage.system.risk import RiskEngine
from arbitrage.system.simulation.exchange import SimulatedExecutionVenue
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState


@pytest.mark.asyncio
async def test_risk_engine_rejects_exposure_breach():
    state = SystemState(10_000)
    risk = RiskEngine(RiskConfig(max_total_exposure_pct=0.1), state)
    plan = AllocationPlan(strategy_allocations={StrategyId.SPOT_ARBITRAGE: 2_000}, total_allocatable_capital=2_000)
    intent = TradeIntent(
        strategy_id=StrategyId.SPOT_ARBITRAGE,
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="htx",
        side="market_neutral",
        confidence=0.8,
        expected_edge_bps=10.0,
        stop_loss_bps=5.0,
    )
    decision = await risk.validate_intent(intent, plan, proposed_notional=1_500, estimated_slippage_bps=2, leverage=1, api_latency_ms=10)
    assert not decision.approved
    assert decision.reason == "total_exposure_cap"


@pytest.mark.asyncio
async def test_risk_engine_global_drawdown_kill_switch():
    state = SystemState(10_000)
    await state.set_equity(8_000)
    risk = RiskEngine(RiskConfig(max_daily_drawdown_pct=0.5, max_portfolio_drawdown_pct=0.1), state)
    plan = AllocationPlan(strategy_allocations={StrategyId.SPOT_ARBITRAGE: 500}, total_allocatable_capital=500)
    intent = TradeIntent(
        strategy_id=StrategyId.SPOT_ARBITRAGE,
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="htx",
        side="market_neutral",
        confidence=0.8,
        expected_edge_bps=10.0,
        stop_loss_bps=5.0,
    )
    decision = await risk.validate_intent(intent, plan, proposed_notional=100, estimated_slippage_bps=1, leverage=1, api_latency_ms=10)
    assert not decision.approved
    assert decision.kill_switch_triggered
    assert decision.reason == "global_drawdown_stop"


@pytest.mark.asyncio
async def test_atomic_execution_dual_success_dry_run():
    monitor = InMemoryMonitoring(logging.getLogger("test"))
    state = SystemState(10_000)
    execution = AtomicExecutionEngine(
        config=ExecutionConfig(dry_run=True),
        venue=SimulatedExecutionVenue(),
        slippage=SlippageModel(),
        state=state,
        monitor=monitor,
    )
    intent = TradeIntent(
        strategy_id=StrategyId.SPOT_ARBITRAGE,
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="htx",
        side="market_neutral",
        confidence=0.7,
        expected_edge_bps=10.0,
        stop_loss_bps=6.0,
        metadata={"entry_mid": 100.0, "long_price": 100.0, "short_price": 101.0},
    )
    report = await execution.execute_dual_entry(intent, 200, 500_000, 0.1, 20)
    assert report.success
    positions = await state.list_positions()
    assert len(positions) == 1


def test_capital_allocator_dynamic_rotation():
    allocator = CapitalAllocator(RiskConfig(max_total_exposure_pct=0.5, max_strategy_allocation_pct=0.3))
    plan = allocator.allocate(
        equity=10_000,
        avg_funding_bps=8.0,
        volatility_regime=0.7,
        trend_strength=0.6,
        enabled=[StrategyId.FUNDING_ARBITRAGE, StrategyId.GRID, StrategyId.INDICATOR],
    )
    assert plan.total_allocatable_capital == 5_000
    assert plan.strategy_allocations[StrategyId.FUNDING_ARBITRAGE] > 0
    assert plan.strategy_allocations[StrategyId.GRID] > 0
    assert plan.strategy_allocations[StrategyId.INDICATOR] > 0
