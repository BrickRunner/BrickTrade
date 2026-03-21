"""WebSocket orderbook cache with task monitoring and auto-reconnect."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

from arbitrage.exchanges.htx_ws import HTXWebSocket
from arbitrage.exchanges.okx_ws import OKXWebSocket
from arbitrage.exchanges.bybit_ws import BybitWebSocket
from arbitrage.exchanges.binance_ws import BinanceWebSocket
from arbitrage.system.models import OrderBookSnapshot

logger = logging.getLogger("trading_system")

_SUPPORTED_EXCHANGES = {"okx", "htx", "bybit", "binance"}


@dataclass
class WsOrderbookCache:
    symbols: Iterable[str]
    exchanges: Iterable[str]
    _orderbooks: Dict[str, Dict[str, OrderBookSnapshot]] = field(default_factory=dict)
    _tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    _stale_after_sec: float = 3.0
    _watchdog_task: Optional[asyncio.Task] = None
    _watchdog_interval_sec: float = 10.0
    _max_restart_attempts: int = 5
    _restart_counts: Dict[str, int] = field(default_factory=dict)
    _invalid_update_counts: Dict[str, int] = field(default_factory=dict)
    _running: bool = False

    async def start(self) -> None:
        self._running = True
        for exchange in self.exchanges:
            if exchange not in _SUPPORTED_EXCHANGES:
                continue
            for symbol in self.symbols:
                key = f"{exchange}:{symbol}"
                task = asyncio.create_task(
                    self._run_ws_with_reconnect(exchange, symbol),
                    name=f"ws_{key}",
                )
                self._tasks[key] = task
        # Start watchdog
        self._watchdog_task = asyncio.create_task(self._watchdog(), name="ws_watchdog")
        logger.info("ws_orderbooks: started %d WS tasks + watchdog", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        for key, task in self._tasks.items():
            if not task.done():
                task.cancel()
        self._tasks.clear()

    async def _run_ws_with_reconnect(self, exchange: str, symbol: str) -> None:
        """Run WS connection with automatic reconnect on failure."""
        key = f"{exchange}:{symbol}"
        while self._running:
            try:
                ws = self._create_ws(exchange, symbol)
                if not ws:
                    return

                async def _on_book(book: Dict) -> None:
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    if not bids or not asks:
                        self._invalid_update_counts[key] = self._invalid_update_counts.get(key, 0) + 1
                        if self._invalid_update_counts[key] % 100 == 1:
                            logger.warning("ws_orderbooks: invalid update #%d for %s (empty bids/asks)",
                                           self._invalid_update_counts[key], key)
                        return
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    if best_bid <= 0 or best_ask <= 0:
                        self._invalid_update_counts[key] = self._invalid_update_counts.get(key, 0) + 1
                        if self._invalid_update_counts[key] % 100 == 1:
                            logger.warning("ws_orderbooks: invalid update #%d for %s (zero price)",
                                           self._invalid_update_counts[key], key)
                        return
                    if best_bid >= best_ask:
                        self._invalid_update_counts[key] = self._invalid_update_counts.get(key, 0) + 1
                        if self._invalid_update_counts[key] % 100 == 1:
                            logger.warning("ws_orderbooks: crossed book #%d for %s (bid=%.6f >= ask=%.6f)",
                                           self._invalid_update_counts[key], key, best_bid, best_ask)
                        return
                    # Prefer server-side timestamp when available;
                    # fall back to local reception time.
                    server_ts = book.get("ts") or book.get("timestamp") or book.get("T")
                    if server_ts is not None:
                        try:
                            ts = float(server_ts)
                            # Exchange timestamps are often in milliseconds
                            if ts > 1e12:
                                ts /= 1000.0
                            # Reject obviously wrong timestamps (>5s in future or >60s old)
                            now_local = time.time()
                            if ts > now_local + 5 or ts < now_local - 60:
                                ts = now_local
                        except (TypeError, ValueError):
                            ts = time.time()
                    else:
                        ts = time.time()
                    snapshot = OrderBookSnapshot(
                        exchange=exchange,
                        symbol=symbol,
                        bid=best_bid,
                        ask=best_ask,
                        timestamp=ts,
                    )
                    self._orderbooks.setdefault(exchange, {})[symbol] = snapshot

                logger.info("ws_orderbooks: connecting %s", key)
                await ws.connect(_on_book)
            except asyncio.CancelledError:
                logger.info("ws_orderbooks: task cancelled for %s", key)
                return
            except Exception as exc:
                self._restart_counts[key] = self._restart_counts.get(key, 0) + 1
                count = self._restart_counts[key]
                if count > self._max_restart_attempts:
                    logger.error("ws_orderbooks: %s exceeded max restarts (%d), giving up", key, self._max_restart_attempts)
                    return
                backoff = min(2 ** count, 30)
                logger.warning("ws_orderbooks: %s disconnected (attempt %d/%d, error=%s), reconnecting in %ds",
                               key, count, self._max_restart_attempts, exc, backoff)
                if self._running:
                    await asyncio.sleep(backoff)

    async def _watchdog(self) -> None:
        """Periodically check WS task health and log status."""
        while self._running:
            try:
                await asyncio.sleep(self._watchdog_interval_sec)
                dead_tasks = []
                alive = 0
                for key, task in self._tasks.items():
                    if task.done():
                        exc = task.exception() if not task.cancelled() else None
                        logger.warning("ws_watchdog: task %s is dead (exception=%s)", key, exc)
                        dead_tasks.append(key)
                    else:
                        alive += 1
                # Restart dead tasks
                for key in dead_tasks:
                    parts = key.split(":", 1)
                    if len(parts) == 2 and self._running:
                        exchange, symbol = parts
                        self._restart_counts[key] = 0  # reset for watchdog-driven restart
                        new_task = asyncio.create_task(
                            self._run_ws_with_reconnect(exchange, symbol),
                            name=f"ws_{key}",
                        )
                        self._tasks[key] = new_task
                        logger.info("ws_watchdog: restarted task %s", key)
                if dead_tasks:
                    logger.info("ws_watchdog: restarted %d dead tasks, %d alive", len(dead_tasks), alive)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("ws_watchdog: error: %s", exc)

    @staticmethod
    def _create_ws(exchange: str, symbol: str):
        if exchange == "okx":
            return OKXWebSocket(symbol=symbol)
        elif exchange == "htx":
            return HTXWebSocket(symbol=symbol)
        elif exchange == "bybit":
            return BybitWebSocket(symbol=symbol)
        elif exchange == "binance":
            return BinanceWebSocket(symbol=symbol)
        return None

    def get(self, exchange: str, symbol: str) -> Optional[OrderBookSnapshot]:
        snapshot = self._orderbooks.get(exchange, {}).get(symbol)
        if not snapshot:
            return None
        if time.time() - snapshot.timestamp > self._stale_after_sec:
            return None
        return snapshot

    def health_status(self) -> Dict[str, Dict]:
        """Return WS health info per exchange:symbol."""
        result = {}
        for key, task in self._tasks.items():
            ob = None
            parts = key.split(":", 1)
            if len(parts) == 2:
                ob = self._orderbooks.get(parts[0], {}).get(parts[1])
            result[key] = {
                "alive": not task.done(),
                "restarts": self._restart_counts.get(key, 0),
                "invalid_updates": self._invalid_update_counts.get(key, 0),
                "last_update_age": round(time.time() - ob.timestamp, 1) if ob else None,
            }
        return result
