from __future__ import annotations

from dataclasses import dataclass
import time

from arbitrage.system.config import RiskConfig
from arbitrage.system.models import AllocationPlan, RiskDecision, TradeIntent
from arbitrage.system.state import SystemState


@dataclass
class RiskEngine:
    config: RiskConfig
    state: SystemState

    def __post_init__(self) -> None:
        self._latency_breach_streak = 0

    async def validate_intent(
        self,
        intent: TradeIntent,
        allocation_plan: AllocationPlan,
        proposed_notional: float,
        estimated_slippage_bps: float,
        leverage: float,
        api_latency_ms: float,
        snapshot=None,
        min_notional_override: bool = False,
    ) -> RiskDecision:
        if await self.state.kill_switch_triggered():
            return RiskDecision(approved=False, reason="kill_switch_active", kill_switch_triggered=True)
        if api_latency_ms > self.config.api_latency_limit_ms:
            self._latency_breach_streak += 1
            if self.config.kill_switch_enabled and self._latency_breach_streak >= self.config.api_latency_breach_limit:
                await self.state.trigger_kill_switch()
                return RiskDecision(approved=False, reason="api_latency_limit_exceeded", kill_switch_triggered=True)
            return RiskDecision(approved=False, reason="api_latency_limit_exceeded")
        self._latency_breach_streak = 0
        if leverage > self.config.max_leverage:
            return RiskDecision(approved=False, reason="leverage_limit_exceeded")
        if estimated_slippage_bps > self.config.max_order_slippage_bps:
            return RiskDecision(approved=False, reason="slippage_limit_exceeded")

        if snapshot is not None:
            max_age = self.config.max_orderbook_age_sec
            if max_age > 0:
                now = time.time()
                for ob in snapshot.orderbooks.values():
                    if now - ob.timestamp > max_age:
                        return RiskDecision(approved=False, reason="stale_orderbook")
                if snapshot.spot_orderbooks:
                    for ob in snapshot.spot_orderbooks.values():
                        if now - ob.timestamp > max_age:
                            return RiskDecision(approved=False, reason="stale_spot_orderbook")
            balances = snapshot.balances or {}
            if balances:
                vals = [v for v in balances.values() if v > 0]
                if vals:
                    max_bal = max(vals)
                    min_bal = min(vals)
                    if min_bal > 0:
                        imbalance = (max_bal - min_bal) / max_bal
                        if imbalance > self.config.max_inventory_imbalance_pct:
                            return RiskDecision(approved=False, reason="inventory_imbalance")

        drawdowns = await self.state.drawdowns()
        if drawdowns["daily_dd"] >= self.config.max_daily_drawdown_pct:
            await self.state.trigger_kill_switch(permanent=True)
            return RiskDecision(approved=False, reason="daily_drawdown_stop", kill_switch_triggered=True)
        if drawdowns["portfolio_dd"] >= self.config.max_portfolio_drawdown_pct:
            await self.state.trigger_kill_switch(permanent=True)
            return RiskDecision(approved=False, reason="global_drawdown_stop", kill_switch_triggered=True)

        snapshot = await self.state.snapshot()
        if snapshot["open_positions"] >= self.config.max_open_positions:
            return RiskDecision(approved=False, reason="max_open_positions_reached")
        max_total_exposure = snapshot["equity"] * self.config.max_total_exposure_pct
        if snapshot["total_exposure"] + proposed_notional > max_total_exposure:
            return RiskDecision(approved=False, reason="total_exposure_cap")

        strategy_cap = allocation_plan.strategy_allocations.get(intent.strategy_id, 0.0)
        if proposed_notional > strategy_cap and not min_notional_override:
            return RiskDecision(approved=False, reason="strategy_allocation_cap")

        return RiskDecision(approved=True, reason="approved")
