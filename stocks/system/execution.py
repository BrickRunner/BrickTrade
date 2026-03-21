from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from stocks.system.config import StockExecutionConfig, StockRiskConfig
from stocks.system.interfaces import StockExecutionVenue
from stocks.system.models import (
    StockExecutionReport,
    StockPosition,
    StockTradeIntent,
)
from stocks.system.state import StockSystemState

logger = logging.getLogger(__name__)

_SIDE_MAP = {"buy": 1, "sell": 2}
_ORDER_TYPE_MAP = {"market": 1, "limit": 2}


@dataclass
class SingleLegExecutionEngine:
    """Executes single-leg stock orders on BCS."""

    config: StockExecutionConfig
    risk_config: StockRiskConfig
    venue: StockExecutionVenue
    state: StockSystemState
    _lock: asyncio.Lock = asyncio.Lock()  # noqa: RUF009

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def execute_entry(
        self, intent: StockTradeIntent, current_price: float
    ) -> StockExecutionReport:
        """Place an entry order and register the position."""
        if self.config.dry_run:
            return await self._simulate_entry(intent, current_price)

        async with self._lock:
            try:
                result = await self.venue.place_order(
                    ticker=intent.ticker,
                    side=intent.side,
                    quantity_lots=intent.quantity_lots,
                    order_type=intent.order_type,
                    limit_price=intent.limit_price or 0.0,
                )
            except Exception as exc:
                logger.error("execution: entry order failed: %s", exc)
                return StockExecutionReport(success=False, message=str(exc))

            if not result.get("order_id"):
                return StockExecutionReport(success=False, message="no_order_id")

            logger.info("execution: order placed result=%s", result)

            # Wait for fill — use BCS-returned orderId if available, fallback to clientOrderId.
            order_id = result.get("orderId", result["order_id"])
            fill_price = await self._wait_fill(order_id, current_price)

            pos_id = str(uuid.uuid4())
            sl_price, tp_price = self._calc_sl_tp(intent, fill_price)
            position = StockPosition(
                position_id=pos_id,
                strategy_id=intent.strategy_id,
                ticker=intent.ticker,
                side=intent.side,
                quantity_lots=intent.quantity_lots,
                entry_price=fill_price,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                peak_price=fill_price,
                trailing_stop_pct=self.risk_config.trailing_stop_pct,
                metadata=intent.metadata,
            )
            await self.state.add_position(position)

            logger.info(
                "execution: opened %s %s %d lots @ %.4f (SL=%.4f TP=%.4f trail=%.1f%%) id=%s",
                intent.side, intent.ticker, intent.quantity_lots,
                fill_price, sl_price, tp_price, self.risk_config.trailing_stop_pct, pos_id,
            )
            return StockExecutionReport(
                success=True,
                position_id=pos_id,
                fill_price=fill_price,
                quantity_lots=intent.quantity_lots,
            )

    async def execute_exit(
        self, position: StockPosition, reason: str, current_price: float
    ) -> StockExecutionReport:
        """Close a position."""
        close_side = "sell" if position.side == "buy" else "buy"

        if self.config.dry_run:
            return await self._simulate_exit(position, reason, current_price)

        async with self._lock:
            try:
                result = await self.venue.place_order(
                    ticker=position.ticker,
                    side=close_side,
                    quantity_lots=position.quantity_lots,
                    order_type="market",
                )
            except Exception as exc:
                logger.error("execution: exit order failed: %s", exc)
                return StockExecutionReport(success=False, message=str(exc))

            # Use real fill price instead of current_price to account for slippage.
            order_id = result.get("order_id", "") if isinstance(result, dict) else ""
            if order_id:
                fill_price = await self._wait_fill(order_id, current_price)
            else:
                fill_price = current_price

            # Check slippage — warn if too large.
            slippage_pct = abs(fill_price - current_price) / current_price * 100 if current_price > 0 else 0
            if slippage_pct > self.risk_config.max_order_slippage_pct:
                logger.warning(
                    "execution: EXIT slippage %.2f%% on %s (expected=%.4f filled=%.4f)",
                    slippage_pct, position.ticker, current_price, fill_price,
                )

            pnl = self._calc_pnl(position, fill_price)
            await self.state.apply_realized_pnl(pnl)
            await self.state.remove_position(position.position_id)

            logger.info(
                "execution: closed %s %s reason=%s fill=%.4f pnl=%.2f slip=%.2f%%",
                position.ticker, position.position_id, reason, fill_price, pnl, slippage_pct,
            )
            return StockExecutionReport(
                success=True,
                position_id=position.position_id,
                fill_price=fill_price,
                quantity_lots=position.quantity_lots,
                message=reason,
            )

    # ------------------------------------------------------------------
    # Dry-run simulation
    # ------------------------------------------------------------------

    async def _simulate_entry(
        self, intent: StockTradeIntent, current_price: float
    ) -> StockExecutionReport:
        fill = current_price
        pos_id = str(uuid.uuid4())
        sl_price, tp_price = self._calc_sl_tp(intent, fill)
        position = StockPosition(
            position_id=pos_id,
            strategy_id=intent.strategy_id,
            ticker=intent.ticker,
            side=intent.side,
            quantity_lots=intent.quantity_lots,
            entry_price=fill,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            peak_price=fill,
            trailing_stop_pct=self.risk_config.trailing_stop_pct,
            metadata=intent.metadata,
        )
        await self.state.add_position(position)
        logger.info(
            "execution[dry]: opened %s %s %d lots @ %.4f (SL=%.4f TP=%.4f trail=%.1f%%) id=%s",
            intent.side, intent.ticker, intent.quantity_lots,
            fill, sl_price, tp_price, self.risk_config.trailing_stop_pct, pos_id,
        )
        return StockExecutionReport(
            success=True, position_id=pos_id,
            fill_price=fill, quantity_lots=intent.quantity_lots,
            message="dry_run",
        )

    async def _simulate_exit(
        self, position: StockPosition, reason: str, current_price: float
    ) -> StockExecutionReport:
        pnl = self._calc_pnl(position, current_price)
        # Dry-run: track simulated P&L but do NOT modify real equity.
        await self.state.apply_simulated_pnl(pnl)
        await self.state.remove_position(position.position_id)
        logger.info(
            "execution[dry]: closed %s reason=%s sim_pnl=%.2f",
            position.position_id, reason, pnl,
        )
        return StockExecutionReport(
            success=True, position_id=position.position_id,
            fill_price=current_price, quantity_lots=position.quantity_lots,
            message=f"dry_run:{reason}",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_fill(self, order_id: str, fallback_price: float) -> float:
        """Poll order status until filled or timeout."""
        deadline = time.monotonic() + self.config.order_timeout_ms / 1000.0
        attempt = 0
        while time.monotonic() < deadline:
            try:
                raw = await self.venue.get_order(order_id)
                attempt += 1

                # BCS nests order data inside "data" key.
                order = raw.get("data", raw) if isinstance(raw, dict) else raw

                if attempt <= 2:
                    logger.info("execution: poll order %s attempt=%d status=%s",
                                order_id, attempt, order.get("orderStatus"))

                status = str(order.get("orderStatus", ""))

                # Filled: orderStatus "2".
                if status == "2":
                    avg_price = order.get("averagePrice", 0)
                    fill = float(avg_price) if avg_price else fallback_price
                    logger.info("execution: order %s FILLED avg=%.4f qty=%.0f commission=%.2f",
                                order_id, fill,
                                order.get("executedQuantity", 0),
                                order.get("commission", 0))
                    return fill

                # Canceled "4" / Rejected "8".
                if status in ("4", "8"):
                    logger.warning("execution: order %s status=%s (cancelled/rejected)", order_id, status)
                    break

            except Exception as exc:
                attempt += 1
                if attempt <= 2:
                    logger.warning("execution: poll order %s error: %s", order_id, exc)
            await asyncio.sleep(0.3)

        logger.warning("execution: order %s not filled in time, using fallback %.4f", order_id, fallback_price)
        return fallback_price

    @staticmethod
    def _calc_sl_tp(intent: StockTradeIntent, fill: float) -> tuple[float, float]:
        if intent.side == "buy":
            sl = fill * (1 - intent.stop_loss_pct / 100)
            tp = fill * (1 + intent.take_profit_pct / 100)
        else:
            sl = fill * (1 + intent.stop_loss_pct / 100)
            tp = fill * (1 - intent.take_profit_pct / 100)
        return round(sl, 4), round(tp, 4)

    @staticmethod
    def _calc_pnl(position: StockPosition, current_price: float) -> float:
        diff = current_price - position.entry_price
        if position.side == "sell":
            diff = -diff
        return diff * position.quantity_lots
