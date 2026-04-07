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
                # Only check imbalance between the TWO exchanges involved in this trade,
                # not all exchanges globally. With 3+ exchanges, global imbalance is
                # naturally high and would block all trades.
                trade_exchanges = [intent.long_exchange, intent.short_exchange]
                trade_vals = [balances.get(ex, 0.0) for ex in trade_exchanges if balances.get(ex, 0.0) > 0]
                if len(trade_vals) == 2:
                    max_bal = max(trade_vals)
                    min_bal = min(trade_vals)
                    imbalance = (max_bal - min_bal) / max_bal
                    if imbalance > self.config.max_inventory_imbalance_pct:
                        return RiskDecision(approved=False, reason="inventory_imbalance")

        drawdowns = await self.state.drawdowns()
        if drawdowns["daily_dd"] >= self.config.max_daily_drawdown_pct:
            # FIX #7: Daily drawdown triggers a TEMPORARY pause (cooldown).
            # The SystemState auto-resets daily DD on midnight, so a permanent
            # kill was contradictory.  Use temporary (cooldown) instead.
            await self.state.trigger_kill_switch(permanent=False)
            return RiskDecision(approved=False, reason="daily_drawdown_stop", kill_switch_triggered=True)
        if drawdowns["portfolio_dd"] >= self.config.max_portfolio_drawdown_pct:
            # Portfolio DD is more serious — keep permanent kill here.
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

        # FIX CRITICAL #3: Position deduplication check.
        # If the same (symbol, long_exchange, short_exchange) combo is already
        # open, reject — prevents compounding risk on the same pair.
        existing = await self.state.list_positions()
        for pos in existing:
            if (
                pos.symbol == intent.symbol
                and pos.long_exchange == intent.long_exchange
                and pos.short_exchange == intent.short_exchange
            ):
                return RiskDecision(
                    approved=False,
                    reason=f"duplicate_position_{intent.symbol}_{intent.long_exchange}_{intent.short_exchange}",
                )

        # FIX AUDIT P1: Exit slippage protection — check historical exit slippage
        # for this symbol. If the average exit slippage on this exchange pair
        # exceeds the slippage limit, reject new entries for this symbol.
        exit_slip = self.state.get_avg_exit_slippage(
            intent.symbol, intent.long_exchange, intent.short_exchange
        )
        if exit_slip is not None and exit_slip > self.config.max_realized_slippage_bps:
            return RiskDecision(
                approved=False,
                reason=f"exit_slippage_too_high: {exit_slip:.1f} bps > {self.config.max_realized_slippage_bps:.1f} bps",
            )

        return RiskDecision(approved=True, reason="approved")
