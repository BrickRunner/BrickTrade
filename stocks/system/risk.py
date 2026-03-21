from __future__ import annotations

import logging
from dataclasses import dataclass

from stocks.system.config import StockRiskConfig
from stocks.system.models import StockRiskDecision, StockTradeIntent
from stocks.system.state import StockSystemState

logger = logging.getLogger(__name__)


@dataclass
class StockRiskEngine:
    """Multi-layer risk validation for stock trades."""

    config: StockRiskConfig
    state: StockSystemState

    async def validate_intent(
        self,
        intent: StockTradeIntent,
        current_price: float,
        cash_available: float = 0.0,
        lot_size: int = 1,
    ) -> StockRiskDecision:
        # Kill-switch
        if await self.state.kill_switch_triggered():
            return StockRiskDecision(
                approved=False, reason="kill_switch_active", kill_switch_triggered=True
            )

        snap = await self.state.snapshot()

        # Daily drawdown
        dd = await self.state.drawdowns()
        if dd["daily_dd"] >= self.config.max_daily_drawdown_pct:
            await self.state.trigger_kill_switch()
            return StockRiskDecision(
                approved=False, reason="daily_drawdown_exceeded", kill_switch_triggered=True
            )

        # Portfolio drawdown
        if dd["portfolio_dd"] >= self.config.max_portfolio_drawdown_pct:
            await self.state.trigger_kill_switch(permanent=True)
            return StockRiskDecision(
                approved=False, reason="portfolio_drawdown_exceeded", kill_switch_triggered=True
            )

        # Position count
        if snap["open_positions"] >= self.config.max_open_positions:
            return StockRiskDecision(approved=False, reason="max_positions_reached")

        # Daily trade count
        if snap["daily_trade_count"] >= self.config.max_daily_trades:
            return StockRiskDecision(approved=False, reason="max_daily_trades_reached")

        if intent.quantity_lots <= 0:
            return StockRiskDecision(
                approved=False, reason="quantity_zero (не хватает на 1 лот)"
            )

        proposed_notional = current_price * intent.quantity_lots * lot_size

        # Use REAL cash for exposure checks (not internal equity tracker).
        real_equity = cash_available if cash_available > 0 else snap["equity"]

        # CASH CHECK — reject if trade costs more than available cash (buy only).
        if intent.side == "buy" and cash_available > 0:
            if proposed_notional > cash_available:
                return StockRiskDecision(
                    approved=False,
                    reason=f"insufficient_cash ({proposed_notional:.0f} > {cash_available:.0f} RUB)",
                )

        # Per-position exposure (% of real cash).
        max_per_pos = real_equity * self.config.max_per_position_pct
        if proposed_notional > max_per_pos:
            return StockRiskDecision(
                approved=False,
                reason=f"per_position_exposure ({proposed_notional:.0f} > {max_per_pos:.0f})",
            )

        # Total exposure (% of real cash).
        max_total = real_equity * self.config.max_total_exposure_pct
        if snap["total_exposure"] + proposed_notional > max_total:
            return StockRiskDecision(
                approved=False,
                reason=f"total_exposure ({snap['total_exposure'] + proposed_notional:.0f} > {max_total:.0f})",
            )

        logger.info(
            "risk: APPROVED %s %s %s notional=%.0f cash=%.0f max_per=%.0f max_total=%.0f",
            intent.strategy_id.value, intent.side, intent.ticker,
            proposed_notional, cash_available, max_per_pos, max_total,
        )
        return StockRiskDecision(approved=True, reason="approved")
