from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.risk import RiskEngine
from arbitrage.system.strategy_runner import StrategyRunner


@dataclass
class BacktestResult:
    trades: int
    accepted: int
    rejected: int
    kill_switch_triggered: bool
    summary: Dict


class BacktestEngine:
    def __init__(
        self,
        symbols: List[str],
        strategy_runner: StrategyRunner,
        risk_engine: RiskEngine,
        allocator: CapitalAllocator,
        execution_engine: AtomicExecutionEngine,
    ):
        self.symbols = symbols
        self.strategy_runner = strategy_runner
        self.risk_engine = risk_engine
        self.allocator = allocator
        self.execution_engine = execution_engine

    async def run(self, provider) -> BacktestResult:
        trades = 0
        accepted = 0
        rejected = 0
        for symbol in self.symbols:
            snapshot = await provider.get_snapshot(symbol)
            state_snapshot = await self.risk_engine.state.snapshot()
            enabled = [s.strategy_id for s in self.strategy_runner.strategies]
            allocation = self.allocator.allocate(
                equity=state_snapshot["equity"],
                avg_funding_bps=max(snapshot.funding_rates.values(), default=0.0) * 10_000,
                volatility_regime=snapshot.volatility,
                trend_strength=snapshot.trend_strength,
                enabled=enabled,
            )
            intents = await self.strategy_runner.generate_intents(snapshot)
            for intent in intents:
                trades += 1
                strategy_budget = allocation.strategy_allocations.get(intent.strategy_id, 0.0)
                proposed_notional = max(0.0, min(strategy_budget, state_snapshot["equity"] * 0.05))
                risk = await self.risk_engine.validate_intent(
                    intent=intent,
                    allocation_plan=allocation,
                    proposed_notional=proposed_notional,
                    estimated_slippage_bps=2.0,
                    leverage=1.0,
                    api_latency_ms=50.0,
                )
                if not risk.approved:
                    rejected += 1
                    continue
                accepted += 1
                await self.execution_engine.execute_dual_entry(
                    intent=intent,
                    notional_usd=proposed_notional,
                    est_book_depth_usd=2_000_000,
                    volatility=snapshot.volatility,
                    latency_ms=40,
                )
        summary = await self.risk_engine.state.snapshot()
        kill_switch = await self.risk_engine.state.kill_switch_triggered()
        return BacktestResult(
            trades=trades,
            accepted=accepted,
            rejected=rejected,
            kill_switch_triggered=kill_switch,
            summary=summary,
        )
