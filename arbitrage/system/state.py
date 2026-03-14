from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List

from arbitrage.system.models import OpenPosition, StrategyId


@dataclass
class EquityPoint:
    ts: float
    equity: float


class SystemState:
    def __init__(self, starting_equity: float):
        self._lock = asyncio.Lock()
        self._starting_equity = starting_equity
        self._equity = starting_equity
        self._daily_start_equity = starting_equity
        self._max_equity = starting_equity
        self._daily_reset_date = date.today()
        self._positions: Dict[str, OpenPosition] = {}
        self._realized_pnl = 0.0
        self._kill_switch = False
        self._kill_switch_ts: float = 0.0
        self._kill_switch_cooldown_sec: float = 120.0
        self._kill_switch_permanent = False
        self._history: List[EquityPoint] = [EquityPoint(ts=time.time(), equity=starting_equity)]

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if today != self._daily_reset_date:
            self._daily_start_equity = self._equity
            self._daily_reset_date = today

    async def set_equity(self, equity: float) -> None:
        async with self._lock:
            self._maybe_reset_daily()
            self._equity = equity
            self._max_equity = max(self._max_equity, equity)
            self._history.append(EquityPoint(ts=time.time(), equity=equity))

    async def apply_realized_pnl(self, pnl: float) -> None:
        async with self._lock:
            self._maybe_reset_daily()
            self._realized_pnl += pnl
            self._equity += pnl
            self._max_equity = max(self._max_equity, self._equity)
            self._history.append(EquityPoint(ts=time.time(), equity=self._equity))

    async def add_position(self, position: OpenPosition) -> None:
        async with self._lock:
            self._positions[position.position_id] = position

    async def remove_position(self, position_id: str) -> OpenPosition | None:
        async with self._lock:
            return self._positions.pop(position_id, None)

    async def list_positions(self) -> List[OpenPosition]:
        async with self._lock:
            return list(self._positions.values())

    async def strategy_exposure(self, strategy_id: StrategyId) -> float:
        async with self._lock:
            return sum(
                p.notional_usd for p in self._positions.values() if p.strategy_id == strategy_id
            )

    async def total_exposure(self) -> float:
        async with self._lock:
            return sum(p.notional_usd for p in self._positions.values())

    async def snapshot(self) -> Dict:
        async with self._lock:
            return {
                "equity": self._equity,
                "max_equity": self._max_equity,
                "daily_start_equity": self._daily_start_equity,
                "open_positions": len(self._positions),
                "total_exposure": sum(p.notional_usd for p in self._positions.values()),
                "realized_pnl": self._realized_pnl,
                "kill_switch": self._kill_switch,
            }

    async def trigger_kill_switch(self, permanent: bool = False) -> None:
        async with self._lock:
            self._kill_switch = True
            self._kill_switch_ts = time.time()
            if permanent:
                self._kill_switch_permanent = True

    async def kill_switch_triggered(self) -> bool:
        async with self._lock:
            if not self._kill_switch:
                return False
            if self._kill_switch_permanent:
                return True
            elapsed = time.time() - self._kill_switch_ts
            if elapsed >= self._kill_switch_cooldown_sec:
                self._kill_switch = False
                return False
            return True

    async def reset_kill_switch(self) -> None:
        async with self._lock:
            self._kill_switch = False
            self._kill_switch_permanent = False
            self._kill_switch_ts = 0.0

    async def drawdowns(self) -> Dict[str, float]:
        async with self._lock:
            dd_total = (self._max_equity - self._equity) / self._max_equity if self._max_equity else 0.0
            dd_daily = (
                (self._daily_start_equity - self._equity) / self._daily_start_equity
                if self._daily_start_equity
                else 0.0
            )
            return {"portfolio_dd": max(0.0, dd_total), "daily_dd": max(0.0, dd_daily)}
