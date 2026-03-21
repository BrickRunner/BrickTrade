from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from stocks.system.models import StockPosition

logger = logging.getLogger(__name__)


class StockSystemState:
    """In-memory state for the stock trading engine.

    Tracks equity (from real API), positions, daily P&L, and kill-switch.
    Simulated (dry-run) P&L is tracked separately and does NOT affect equity.
    """

    def __init__(self, starting_equity: float) -> None:
        self._lock = asyncio.Lock()
        self._starting_equity = starting_equity
        self._equity = starting_equity          # Synced from real portfolio via set_equity()
        self._max_equity = starting_equity
        self._positions: Dict[str, StockPosition] = {}
        self._realized_pnl = 0.0
        self._daily_realized_pnl = 0.0
        self._simulated_pnl = 0.0               # Dry-run P&L (does not touch equity)
        self._daily_simulated_pnl = 0.0
        self._daily_trade_count = 0
        self._kill_switch = False
        self._kill_switch_permanent = False
        self._day_mark: str = time.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Equity (synced from real API balance)
    # ------------------------------------------------------------------

    async def set_equity(self, equity: float, reset_peak: bool = False) -> None:
        """Sync equity from real portfolio.

        Args:
            reset_peak: If True, also reset max_equity (use on initial sync).
        """
        async with self._lock:
            self._equity = equity
            if reset_peak or equity > self._max_equity:
                self._max_equity = equity

    @property
    def equity(self) -> float:
        return self._equity

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def _maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day_mark:
            self._day_mark = today
            self._daily_realized_pnl = 0.0
            self._daily_simulated_pnl = 0.0
            self._daily_trade_count = 0
            if not self._kill_switch_permanent:
                self._kill_switch = False

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def add_position(self, position: StockPosition) -> None:
        async with self._lock:
            self._maybe_reset_daily()
            self._positions[position.position_id] = position
            self._daily_trade_count += 1

    async def remove_position(self, position_id: str) -> Optional[StockPosition]:
        async with self._lock:
            return self._positions.pop(position_id, None)

    async def list_positions(self) -> List[StockPosition]:
        async with self._lock:
            return list(self._positions.values())

    async def get_position(self, position_id: str) -> Optional[StockPosition]:
        async with self._lock:
            return self._positions.get(position_id)

    async def positions_for_ticker(self, ticker: str) -> List[StockPosition]:
        async with self._lock:
            return [p for p in self._positions.values() if p.ticker == ticker]

    # ------------------------------------------------------------------
    # P&L
    # ------------------------------------------------------------------

    async def apply_realized_pnl(self, pnl: float) -> None:
        """Apply REAL P&L — affects equity."""
        async with self._lock:
            self._realized_pnl += pnl
            self._daily_realized_pnl += pnl
            self._equity += pnl
            if self._equity > self._max_equity:
                self._max_equity = self._equity

    async def apply_simulated_pnl(self, pnl: float) -> None:
        """Apply simulated (dry-run) P&L — does NOT affect equity."""
        async with self._lock:
            self._simulated_pnl += pnl
            self._daily_simulated_pnl += pnl

    # ------------------------------------------------------------------
    # Drawdown
    # ------------------------------------------------------------------

    async def drawdowns(self) -> Dict[str, float]:
        async with self._lock:
            self._maybe_reset_daily()
            portfolio_dd = (
                (self._max_equity - self._equity) / self._max_equity
                if self._max_equity > 0
                else 0.0
            )
            daily_dd = (
                abs(min(0.0, self._daily_realized_pnl)) / self._equity
                if self._equity > 0
                else 0.0
            )
            return {"portfolio_dd": portfolio_dd, "daily_dd": daily_dd}

    # ------------------------------------------------------------------
    # Kill-switch
    # ------------------------------------------------------------------

    async def trigger_kill_switch(self, permanent: bool = False) -> None:
        async with self._lock:
            self._kill_switch = True
            if permanent:
                self._kill_switch_permanent = True
            logger.warning("stock_state: kill-switch triggered (permanent=%s)", permanent)

    async def kill_switch_triggered(self) -> bool:
        async with self._lock:
            self._maybe_reset_daily()
            return self._kill_switch

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            self._maybe_reset_daily()
            return {
                "equity": self._equity,
                "starting_equity": self._starting_equity,
                "max_equity": self._max_equity,
                "open_positions": len(self._positions),
                "total_exposure": sum(
                    p.entry_price * p.quantity_lots for p in self._positions.values()
                ),
                "realized_pnl": self._realized_pnl,
                "daily_realized_pnl": self._daily_realized_pnl,
                "simulated_pnl": self._simulated_pnl,
                "daily_simulated_pnl": self._daily_simulated_pnl,
                "daily_trade_count": self._daily_trade_count,
                "kill_switch": self._kill_switch,
            }
