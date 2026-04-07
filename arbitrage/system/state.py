from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Dict, List

import aiofiles

from arbitrage.system.models import OpenPosition, StrategyId

logger = logging.getLogger("trading_system")

_POSITIONS_FILE = os.getenv("POSITIONS_FILE", "data/open_positions.json")


@dataclass
class EquityPoint:
    ts: float
    equity: float


def _serialize_position(pos: OpenPosition) -> dict:
    return {
        "position_id": pos.position_id,
        "strategy_id": pos.strategy_id.value,
        "symbol": pos.symbol,
        "long_exchange": pos.long_exchange,
        "short_exchange": pos.short_exchange,
        "notional_usd": pos.notional_usd,
        "entry_mid": pos.entry_mid,
        "stop_loss_bps": pos.stop_loss_bps,
        "opened_at": pos.opened_at,
        "realized_pnl": pos.realized_pnl,
        "unrealized_pnl": pos.unrealized_pnl,
        "metadata": {k: v for k, v in pos.metadata.items() if isinstance(v, (str, int, float, bool, type(None)))},
    }


def _deserialize_position(data: dict) -> OpenPosition:
    return OpenPosition(
        position_id=data["position_id"],
        strategy_id=StrategyId(data["strategy_id"]),
        symbol=data["symbol"],
        long_exchange=data["long_exchange"],
        short_exchange=data["short_exchange"],
        notional_usd=float(data["notional_usd"]),
        entry_mid=float(data["entry_mid"]),
        stop_loss_bps=float(data["stop_loss_bps"]),
        opened_at=float(data.get("opened_at", time.time())),
        realized_pnl=float(data.get("realized_pnl", 0.0)),
        unrealized_pnl=float(data.get("unrealized_pnl", 0.0)),
        metadata=data.get("metadata", {}),
    )


class SystemState:
    def __init__(self, starting_equity: float, positions_file: str | None = None):
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
        self._positions_file = positions_file or _POSITIONS_FILE
        # FIX CRITICAL A: Persistent kill switch state across restarts
        self._kill_switch_file = self._positions_file.replace(".json", "_killswitch.json") if self._positions_file else None
        if self._positions_file != ":memory:":
            self._load_positions()
            self._load_kill_switch_state()

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if today != self._daily_reset_date:
            self._daily_start_equity = self._equity
            self._daily_reset_date = today
            # Auto-reset kill switch on new day (daily drawdown resets)
            # FIX CRITICAL A: Do NOT auto-reset permanent kill switch —
            # permanent kill switch requires explicit manual reset.
            if not self._kill_switch_permanent:
                self._kill_switch = False
                self._kill_switch_ts = 0.0

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
            self._persist_positions()

    async def remove_position(self, position_id: str) -> OpenPosition | None:
        async with self._lock:
            pos = self._positions.pop(position_id, None)
            if pos is not None:
                self._persist_positions()
            return pos

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
        # FIX CRITICAL A: Persist kill switch state to disk
        self._persist_kill_switch()

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
        # FIX CRITICAL A: Persist cleared kill switch state to disk
        self._persist_kill_switch()

    async def drawdowns(self) -> Dict[str, float]:
        async with self._lock:
            dd_total = (self._max_equity - self._equity) / self._max_equity if self._max_equity else 0.0
            dd_daily = (
                (self._daily_start_equity - self._equity) / self._daily_start_equity
                if self._daily_start_equity
                else 0.0
            )
            return {"portfolio_dd": max(0.0, dd_total), "daily_dd": max(0.0, dd_daily)}

    def _load_positions(self) -> None:
        """Synchronous load at startup (before event loop is running)."""
        try:
            if os.path.exists(self._positions_file):
                with open(self._positions_file, "r") as f:
                    data = json.load(f)
                for item in data:
                    pos = _deserialize_position(item)
                    self._positions[pos.position_id] = pos
                if self._positions:
                    logger.info("Recovered %d open positions from %s", len(self._positions), self._positions_file)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load positions from %s (corrupted data): %s", self._positions_file, exc)
        except OSError as exc:
            logger.warning("Failed to load positions from %s (I/O error): %s", self._positions_file, exc)

    async def _persist_positions_async(self) -> None:
        """Non-blocking async file persistence — called from async context."""
        if self._positions_file == ":memory:":
            return
        try:
            os.makedirs(os.path.dirname(self._positions_file) or ".", exist_ok=True)
            data = [_serialize_position(p) for p in self._positions.values()]
            payload = json.dumps(data, indent=2)
            tmp = self._positions_file + ".tmp"
            async with aiofiles.open(tmp, "w") as f:
                await f.write(payload)
            os.replace(tmp, self._positions_file)
        except Exception as exc:
            logger.warning("Failed to persist positions: %s", exc)

    def _persist_positions(self) -> None:
        """Schedule async persistence without blocking the caller."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._persist_positions_async())
        except RuntimeError:
            # No running loop (e.g. during tests) — fall back to sync
            if self._positions_file == ":memory:":
                return
            try:
                os.makedirs(os.path.dirname(self._positions_file) or ".", exist_ok=True)
                data = [_serialize_position(p) for p in self._positions.values()]
                tmp = self._positions_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self._positions_file)
            except Exception as exc:
                logger.warning("Failed to persist positions (sync fallback): %s", exc)

    # ─── FIX CRITICAL A: Kill switch persistence ────────────────────────

    def _load_kill_switch_state(self) -> None:
        """Load kill switch state from disk on startup. Ensures kill switch
        survives process restarts and isn't silently cleared."""
        if not self._kill_switch_file:
            return
        try:
            if os.path.exists(self._kill_switch_file):
                with open(self._kill_switch_file, "r") as f:
                    data = json.load(f)
                self._kill_switch = data.get("kill_switch", False)
                self._kill_switch_ts = float(data.get("kill_switch_ts", 0.0))
                self._kill_switch_permanent = data.get("kill_switch_permanent", False)
                if self._kill_switch:
                    logger.warning(
                        "Kill switch LOADED from disk: active=%s permanent=%s ts=%.0f",
                        self._kill_switch, self._kill_switch_permanent, self._kill_switch_ts,
                    )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load kill switch state (corrupted data): %s", exc)
        except OSError as exc:
            logger.warning("Failed to load kill switch state (I/O error): %s", exc)

    def _persist_kill_switch(self) -> None:
        """Atomically persist kill switch state to disk."""
        if not self._kill_switch_file:
            return
        try:
            os.makedirs(os.path.dirname(self._kill_switch_file) or ".", exist_ok=True)
            data = {
                "kill_switch": self._kill_switch,
                "kill_switch_ts": self._kill_switch_ts,
                "kill_switch_permanent": self._kill_switch_permanent,
            }
            tmp = self._kill_switch_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._kill_switch_file)
        except Exception as exc:
            logger.warning("Failed to persist kill switch: %s", exc)
