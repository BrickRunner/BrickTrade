from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from stocks.system.config import StockTradingConfig
from stocks.system.confirmation import SemiAutoConfirmationManager
from stocks.system.execution import SingleLegExecutionEngine
from stocks.system.models import StockSnapshot, StockTradeIntent
from stocks.system.risk import StockRiskEngine
from stocks.system.schedule import MOEXSchedule
from stocks.system.state import StockSystemState
from stocks.system.strategy_runner import StockStrategyRunner

logger = logging.getLogger(__name__)


_BALANCE_LOG_INTERVAL = 60  # seconds


@dataclass
class StockTradingEngine:
    """Main cycle-based trading engine for MOEX stocks via BCS."""

    config: StockTradingConfig
    provider: object  # StockMarketDataProvider (duck-typed)
    risk: StockRiskEngine
    execution: SingleLegExecutionEngine
    strategies: StockStrategyRunner
    state: StockSystemState
    schedule: MOEXSchedule = field(default_factory=MOEXSchedule)
    confirmation: Optional[SemiAutoConfirmationManager] = None
    user_id: int = 0
    _cycle_count: int = 0
    _last_balance_log: float = 0.0
    _signal_cooldowns: Dict[str, float] = field(default_factory=dict)  # "strategy:ticker" -> timestamp

    async def run_forever(self) -> None:
        """Main loop — runs cycles during MOEX trading hours, sleeps otherwise."""
        logger.info("stock_engine: starting (mode=%s)", self.config.execution.mode)
        while True:
            try:
                if not self.schedule.is_trading_hours():
                    nxt = self.schedule.next_session_open()
                    logger.info("stock_engine: market closed, next open ~%s", nxt)
                    await asyncio.sleep(min(60, max(1, self.schedule.seconds_until_close() or 60)))
                    continue

                await self.run_cycle()
            except asyncio.CancelledError:
                logger.info("stock_engine: cancelled")
                raise
            except Exception as exc:
                logger.error("stock_engine: cycle error: %s", exc, exc_info=True)

            await asyncio.sleep(self.config.execution.cycle_interval_seconds)

    async def _log_balance(self) -> None:
        """Log available balance every minute and sync state equity with real portfolio."""
        now = time.time()
        if now - self._last_balance_log < _BALANCE_LOG_INTERVAL:
            return
        self._last_balance_log = now
        try:
            snapshot = await self.provider.get_snapshot(self.config.tickers[0])  # type: ignore[attr-defined]
            # Sync internal equity tracker with real portfolio value.
            if snapshot.portfolio_value > 0:
                await self.state.set_equity(snapshot.portfolio_value)
            logger.info(
                "stock_engine: BALANCE portfolio=%.2f RUB | cash=%.2f RUB | tickers=%d | cycle=#%d",
                snapshot.portfolio_value,
                snapshot.cash_available,
                len(self.config.tickers),
                self._cycle_count,
            )
        except Exception as exc:
            logger.warning("stock_engine: balance log failed: %s", exc)

    async def run_cycle(self) -> None:
        self._cycle_count += 1

        # 0. On first cycle, immediately sync equity from real portfolio.
        if self._cycle_count == 1:
            self._last_balance_log = 0.0  # Force immediate balance log

        # 0. Log balance periodically.
        await self._log_balance()

        # 1. Process existing open positions (SL / TP / time stop).
        await self._process_open_positions()

        # 2. Check kill-switch.
        if await self.state.kill_switch_triggered():
            return

        # 3. For each ticker, generate + route intents.
        for ticker in self.config.tickers:
            try:
                snapshot: StockSnapshot = await self.provider.get_snapshot(ticker)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("stock_engine: snapshot failed for %s: %s", ticker, exc)
                continue

            intents = await self.strategies.generate_intents(snapshot)

            # --- Quality filter: reject low-confidence and low-edge signals ---
            min_conf = self.config.risk.min_confidence
            min_edge = self.config.risk.min_edge_pct
            raw_count = len(intents)
            intents = [
                i for i in intents
                if i.confidence >= min_conf and i.expected_edge_pct >= min_edge
            ]

            # Sort by confidence descending.
            intents.sort(key=lambda i: i.confidence, reverse=True)

            if raw_count > 0:
                logger.info(
                    "stock_engine: %s -> %d signal(s) (%d filtered): %s",
                    ticker,
                    len(intents),
                    raw_count - len(intents),
                    ", ".join(f"{i.strategy_id.value} {i.side} conf={i.confidence:.2f} edge={i.expected_edge_pct:.2f}%" for i in intents[:3]),
                )

            for intent in intents:
                # Adjust quantity to lot_size and available cash.
                intent = self._adjust_quantity(intent, snapshot)
                if await self.state.kill_switch_triggered():
                    return

                # Risk check — pass real cash balance and lot size.
                decision = await self.risk.validate_intent(
                    intent, snapshot.quote.last,
                    cash_available=snapshot.cash_available,
                    lot_size=snapshot.lot_size,
                )
                if not decision.approved:
                    # Only log non-cash rejections at INFO (cash rejections are expected).
                    if "insufficient_cash" in (decision.reason or ""):
                        logger.debug(
                            "stock_engine: skip %s %s (price %.0f > cash): %s",
                            intent.side, ticker, snapshot.quote.last, decision.reason,
                        )
                    else:
                        logger.info(
                            "stock_engine: RISK REJECTED %s %s %s: %s",
                            intent.strategy_id.value, intent.side, ticker, decision.reason,
                        )
                    continue

                # Cooldown — don't send the same signal too frequently.
                cooldown_key = f"{intent.strategy_id.value}:{ticker}:{intent.side}"
                now = time.time()
                last_sent = self._signal_cooldowns.get(cooldown_key, 0)
                cooldown_sec = self.config.risk.signal_cooldown_sec
                if now - last_sent < cooldown_sec:
                    continue
                self._signal_cooldowns[cooldown_key] = now

                await self._route_intent(intent, snapshot)

    @staticmethod
    def _adjust_quantity(intent: StockTradeIntent, snapshot: StockSnapshot) -> StockTradeIntent:
        """Recalculate quantity_lots based on lot_size and available cash.

        Returns a new intent with the correct quantity, or the original if
        the stock is too expensive for even 1 lot.
        """
        lot_size = snapshot.lot_size
        price = snapshot.quote.last
        cash = snapshot.cash_available

        if price <= 0 or lot_size <= 0:
            return intent

        # Cost of 1 lot = price * lot_size.
        cost_per_lot = price * lot_size

        if intent.side == "buy" and cash > 0:
            # How many lots can we afford?
            max_lots = int(cash / cost_per_lot) if cost_per_lot > 0 else 0
            if max_lots <= 0:
                # Can't even buy 1 lot — return with qty=0, will be filtered by risk.
                return StockTradeIntent(
                    strategy_id=intent.strategy_id, ticker=intent.ticker,
                    side=intent.side, quantity_lots=0,
                    order_type=intent.order_type, limit_price=intent.limit_price,
                    confidence=intent.confidence, expected_edge_pct=intent.expected_edge_pct,
                    stop_loss_pct=intent.stop_loss_pct, take_profit_pct=intent.take_profit_pct,
                    mode=intent.mode, metadata=intent.metadata,
                )
            qty = min(intent.quantity_lots, max_lots)
        else:
            qty = intent.quantity_lots

        # Ensure quantity is a multiple of lot_size.
        if lot_size > 1:
            qty = max(1, qty)  # At least 1 lot.

        if qty == intent.quantity_lots:
            return intent

        return StockTradeIntent(
            strategy_id=intent.strategy_id, ticker=intent.ticker,
            side=intent.side, quantity_lots=qty,
            order_type=intent.order_type, limit_price=intent.limit_price,
            confidence=intent.confidence, expected_edge_pct=intent.expected_edge_pct,
            stop_loss_pct=intent.stop_loss_pct, take_profit_pct=intent.take_profit_pct,
            mode=intent.mode, metadata=intent.metadata,
        )

    async def _route_intent(
        self, intent: StockTradeIntent, snapshot: StockSnapshot
    ) -> None:
        mode = self.config.execution.mode

        if mode == "monitoring":
            logger.info(
                "stock_engine[monitor]: %s %s %s conf=%.2f edge=%.2f%% price=%.2f",
                intent.strategy_id.value, intent.side, intent.ticker,
                intent.confidence, intent.expected_edge_pct, snapshot.quote.last,
            )
            return

        if mode == "semi_auto":
            # Semi-auto: send confirmation to user, wait for approval.
            if self.confirmation and self.user_id:
                logger.info(
                    "stock_engine[semi_auto]: подтверждение %s %s %s conf=%.2f price=%.2f",
                    intent.strategy_id.value, intent.side, intent.ticker,
                    intent.confidence, snapshot.quote.last,
                )
                asyncio.create_task(
                    self._confirm_and_execute(intent, snapshot.quote.last, snapshot.lot_size)
                )
            else:
                logger.warning(
                    "stock_engine: semi_auto but no confirmation manager "
                    "(confirmation=%s, user_id=%s)",
                    self.confirmation is not None, self.user_id,
                )
            return

        # Auto mode — execute immediately without confirmation.
        logger.info(
            "stock_engine[auto]: исполняю %s %s %s conf=%.2f price=%.2f",
            intent.strategy_id.value, intent.side, intent.ticker,
            intent.confidence, snapshot.quote.last,
        )
        await self._execute_intent(intent, snapshot.quote.last)

    async def _confirm_and_execute(
        self, intent: StockTradeIntent, price: float, lot_size: int = 1,
    ) -> None:
        """Background task: send confirmation, wait for user, execute if confirmed."""
        try:
            logger.info(
                "stock_engine: ожидаю подтверждение пользователя для %s %s %s...",
                intent.strategy_id.value, intent.side, intent.ticker,
            )
            confirmed = await self.confirmation.request_confirmation(
                intent, self.user_id, price, lot_size=lot_size,
            )
            if confirmed is None:
                logger.info("stock_engine: отклонено/таймаут %s %s", intent.strategy_id.value, intent.ticker)
                return

            logger.info(
                "stock_engine: ПОДТВЕРЖДЕНО пользователем, исполняю %s %s %s",
                confirmed.strategy_id.value, confirmed.side, confirmed.ticker,
            )

            # Re-fetch fresh price before executing.
            try:
                snap = await self.provider.get_snapshot(intent.ticker)  # type: ignore[attr-defined]
                exec_price = snap.quote.last
            except Exception:
                exec_price = price

            await self._execute_intent(confirmed, exec_price)
        except Exception as exc:
            logger.error("stock_engine: confirm_and_execute error: %s", exc, exc_info=True)

    async def _execute_intent(self, intent: StockTradeIntent, price: float) -> None:
        """Execute a single trade intent."""
        report = await self.execution.execute_entry(intent, price)
        if report.success:
            logger.info(
                "stock_engine: EXECUTED %s %s %s @ %.4f id=%s",
                intent.strategy_id.value, intent.side, intent.ticker,
                report.fill_price, report.position_id,
            )
        else:
            logger.warning(
                "stock_engine: execution failed %s %s: %s",
                intent.ticker, intent.strategy_id.value, report.message,
            )

    async def _process_open_positions(self) -> None:
        """Check trailing stop / SL / TP / time for all open positions."""
        trailing_pct = self.config.risk.trailing_stop_pct
        positions = await self.state.list_positions()
        for pos in positions:
            try:
                snapshot: StockSnapshot = await self.provider.get_snapshot(pos.ticker)  # type: ignore[attr-defined]
            except Exception:
                continue

            price = snapshot.quote.last
            if price <= 0:
                continue

            # --- Trailing stop: update peak and move SL ---
            if trailing_pct > 0:
                if pos.side == "buy":
                    if price > pos.peak_price:
                        pos.peak_price = price
                    new_sl = pos.peak_price * (1 - trailing_pct / 100)
                    if new_sl > pos.stop_loss_price:
                        pos.stop_loss_price = round(new_sl, 4)
                else:  # sell
                    if pos.peak_price == 0 or price < pos.peak_price:
                        pos.peak_price = price
                    new_sl = pos.peak_price * (1 + trailing_pct / 100)
                    if new_sl < pos.stop_loss_price:
                        pos.stop_loss_price = round(new_sl, 4)

            # Stop-loss.
            if pos.side == "buy" and price <= pos.stop_loss_price:
                await self.execution.execute_exit(pos, "stop_loss", price)
                continue
            if pos.side == "sell" and price >= pos.stop_loss_price:
                await self.execution.execute_exit(pos, "stop_loss", price)
                continue

            # Take-profit.
            if pos.side == "buy" and price >= pos.take_profit_price:
                await self.execution.execute_exit(pos, "take_profit", price)
                continue
            if pos.side == "sell" and price <= pos.take_profit_price:
                await self.execution.execute_exit(pos, "take_profit", price)
                continue

            # Time stop — close positions held > 4 hours.
            age = time.time() - pos.opened_at
            if age > 4 * 3600:
                await self.execution.execute_exit(pos, "time_stop", price)
                continue

            # End-of-session stop — close 5 min before session ends.
            secs_left = self.schedule.seconds_until_close()
            if 0 < secs_left < 300:
                await self.execution.execute_exit(pos, "session_close", price)
