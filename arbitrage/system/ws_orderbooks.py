"""WebSocket orderbook cache with task monitoring and auto-reconnect."""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

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
    # Full depth: {exchange: {symbol: {"bids": [...], "asks": [...]}}}
    _depth: Dict[str, Dict[str, Dict[str, list]]] = field(default_factory=dict)
    _tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    # FIX CRITICAL #6: Async lock to protect concurrent reads/writes to
    # _orderbooks and _depth from multiple WS callback tasks.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # FIX CRITICAL #5: Track active WS client instances for liveness checks.
    _ws_instances: Dict[str, Any] = field(default_factory=dict)
    _stale_after_sec: float = 3.0
    _watchdog_task: Optional[asyncio.Task] = None
    _watchdog_interval_sec: float = 10.0
    _max_restart_attempts: int = 5
    # FIX #5: Soft cap — after max attempts the task gives up, but the
    # watchdog can resurrect it indefinitely after network recovery.
    _restart_counts: Dict[str, int] = field(default_factory=dict)
    _restart_last_ts: Dict[str, float] = field(default_factory=dict)
    _restart_decay_sec: float = 300.0  # decay restart count after 5 min of stability
    _invalid_update_counts: Dict[str, int] = field(default_factory=dict)
    _max_depth_symbols: int = 200  # max depth entries to prevent unbounded growth
    _running: bool = False
    # FIX AUDIT P2: Orderbook sequence number tracking per exchange:symbol.
    # Detects gaps in incremental WS updates that produce corrupted books.
    _seq_numbers: Dict[str, int] = field(default_factory=dict)
    _last_update_ts: Dict[str, float] = field(default_factory=dict)

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
        # FIX: Close all WS instances cleanly
        for ws in self._ws_instances.values():
            if ws and hasattr(ws, "disconnect"):
                try:
                    await asyncio.wait_for(ws.disconnect(), timeout=2.0)
                except Exception:
                    pass
        self._ws_instances.clear()

    async def _run_ws_with_reconnect(self, exchange: str, symbol: str) -> None:
        """Run WS connection with automatic reconnect on failure."""
        key = f"{exchange}:{symbol}"
        while self._running:
            ws = None
            try:
                ws = self._create_ws(exchange, symbol)
                if not ws:
                    return

                # FIX CRITICAL #5: Register the WS instance so watchdog
                # can check its health later.
                async with self._lock:
                    self._ws_instances[key] = ws

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
                    # FIX AUDIT P2: Sequence number validation. Some exchanges
                    # (Binance, OKX) send lastUpdateId/pubId in each message.
                    # Track and validate for gaps.
                    seq = book.get("lastUpdateId") or book.get("pubId") or book.get("u") or book.get("U")
                    async with self._lock:
                        if seq is not None:
                            try:
                                seq_int = int(seq)
                                prev_seq = self._seq_numbers.get(key)
                                if prev_seq is not None and seq_int <= prev_seq:
                                    # Stale/duplicate update — skip
                                    if self._invalid_update_counts.get(key, 0) % 200 == 0:
                                        logger.debug(
                                            "ws_orderbooks: stale seq for %s (prev=%d, cur=%d), skipping",
                                            key, prev_seq, seq_int,
                                        )
                                    self._invalid_update_counts[key] = self._invalid_update_counts.get(key, 0) + 1
                                    # Don't return here — still update timestamp
                                elif prev_seq is not None and seq_int > prev_seq + 1000:
                                    # Gap > 1000 = likely data loss, invalidate book
                                    logger.warning(
                                        "ws_orderbooks: seq gap for %s (prev=%d, cur=%d), "
                                        "invalidating book — will trigger REST re-sync",
                                        key, prev_seq, seq_int,
                                    )
                                    self._orderbooks.get(exchange, {}).pop(symbol, None)
                                    self._depth.get(exchange, {}).pop(symbol, None)
                                self._seq_numbers[key] = seq_int
                            except (ValueError, TypeError):
                                pass

                        snapshot = OrderBookSnapshot(
                            exchange=exchange,
                            symbol=symbol,
                            bid=best_bid,
                            ask=best_ask,
                            timestamp=ts,
                        )
                        # FIX CRITICAL #6: Protect concurrent writes with lock.
                        self._orderbooks.setdefault(exchange, {})[symbol] = snapshot
                        # Store full depth for walk-the-book calculations
                        # Make a copy to avoid external mutation
                        self._depth.setdefault(exchange, {})[symbol] = {
                            "bids": copy.deepcopy(bids),
                            "asks": copy.deepcopy(asks),
                        }
                        # FIX AUDIT P0: Track last update timestamp for stale detection
                        self._last_update_ts[key] = time.time()

                logger.info("ws_orderbooks: connecting %s", key)
                await ws.connect(_on_book)
            except asyncio.CancelledError:
                logger.info("ws_orderbooks: task cancelled for %s", key)
                return
            except Exception as exc:
                # Decay restart count if feed was stable for a while
                now = time.time()
                last_restart = self._restart_last_ts.get(key, 0.0)
                if now - last_restart > self._restart_decay_sec and self._restart_counts.get(key, 0) > 0:
                    self._restart_counts[key] = max(0, self._restart_counts.get(key, 0) - 1)
                self._restart_counts[key] = self._restart_counts.get(key, 0) + 1
                self._restart_last_ts[key] = now
                count = self._restart_counts[key]
                if count > self._max_restart_attempts:
                    logger.error("ws_orderbooks: %s exceeded max restarts (%d), giving up", key, self._max_restart_attempts)
                    return
                backoff = min(2 ** count, 30)
                logger.warning("ws_orderbooks: %s disconnected (attempt %d/%d, error=%s), reconnecting in %ds",
                               key, count, self._max_restart_attempts, exc, backoff)
                if self._running:
                    await asyncio.sleep(backoff)
            finally:
                # FIX CRITICAL #9: Clean up WS instance on exit to prevent leaks.
                async with self._lock:
                    old_ws = self._ws_instances.pop(key, None)
                if old_ws and hasattr(old_ws, "disconnect"):
                    try:
                        await asyncio.wait_for(old_ws.disconnect(), timeout=1.0)
                    except Exception:
                        pass

    async def _watchdog(self) -> None:
        """Periodically check WS task health and log status.

        FIX CRITICAL #5: Also detect stale feeds where task is alive
        but no messages flow (silent death). Force reconnect.
        RESURRECTION: Watchdog can now restart tasks even after they
        exceeded max_restart_attempts — network recovery is handled.
        """
        while self._running:
            try:
                await asyncio.sleep(self._watchdog_interval_sec)
                dead_tasks = []
                stale_tasks = []
                exhausted_tasks = []
                alive = 0
                for key, task in self._tasks.items():
                    if task.done():
                        exc = task.exception() if not task.cancelled() else None
                        logger.warning("ws_watchdog: task %s is dead (exception=%s)", key, exc)
                        dead_tasks.append(key)
                    else:
                        # FIX: Detect stale feeds — task alive but orderbook too old
                        parts = key.split(":", 1)
                        if len(parts) == 2:
                            ob_age = self._orderbook_age_sync(parts[0], parts[1])
                            if ob_age is not None and ob_age > 30.0:
                                logger.warning(
                                    "ws_watchdog: task %s alive but feed stale (%.0fs old)",
                                    key, ob_age,
                                )
                                stale_tasks.append(key)
                            else:
                                alive += 1
                        else:
                            alive += 1
                # FIX #5: Detect tasks that gave up after max restart attempts
                # (exhausted) — the watchdog can resurrect them.
                for key, count in list(self._restart_counts.items()):
                    if count > self._max_restart_attempts:
                        task = self._tasks.get(key)
                        if task and task.done() and not task.cancelled():
                            exhausted_tasks.append(key)

                # Restart dead, stale, and exhausted tasks
                for key in dead_tasks + stale_tasks + exhausted_tasks:
                    parts = key.split(":", 1)
                    if len(parts) == 2 and self._running:
                        exchange, symbol = parts
                        self._restart_counts[key] = 0  # reset for watchdog-driven restart
                        new_task = asyncio.create_task(
                            self._run_ws_with_reconnect(exchange, symbol),
                            name=f"ws_{key}",
                        )
                        self._tasks[key] = new_task
                        logger.info("ws_watchdog: resurrected task %s", key)

                # Clean stale depth entries for dead feeds
                for key in dead_tasks + exhausted_tasks:
                    parts_d = key.split(":", 1)
                    if len(parts_d) == 2:
                        ex, sym = parts_d
                        async with self._lock:
                            self._depth.get(ex, {}).pop(sym, None)
                            self._orderbooks.get(ex, {}).pop(sym, None)

                if dead_tasks or exhausted_tasks or stale_tasks:
                    logger.info(
                        "ws_watchdog: restarted %d dead, %d stale, %d exhausted tasks, %d alive",
                        len(dead_tasks), len(stale_tasks), len(exhausted_tasks), alive,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("ws_watchdog: error: %s", exc)

    async def _orderbook_age_sync(self, exchange: str, symbol: str) -> Optional[float]:
        """Thread-safe read of orderbook age."""
        async with self._lock:
            ob = self._orderbooks.get(exchange, {}).get(symbol)
        if ob:
            return time.time() - ob.timestamp
        return None

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

    async def get(self, exchange: str, symbol: str) -> Optional[OrderBookSnapshot]:
        # FIX CRITICAL #6: Lock-protected read
        async with self._lock:
            snapshot = self._orderbooks.get(exchange, {}).get(symbol)
        if not snapshot:
            return None
        if time.time() - snapshot.timestamp > self._stale_after_sec:
            return None
        return snapshot

    get_sync = get  # alias for compatibility

    async def get_depth(self, exchange: str, symbol: str) -> Optional[Dict[str, list]]:
        """Return full orderbook depth from WS feed.

        Returns {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        or None if stale/unavailable.
        """
        async with self._lock:
            snapshot = self._orderbooks.get(exchange, {}).get(symbol)
            if not snapshot:
                return None
            if time.time() - snapshot.timestamp > self._stale_after_sec:
                return None
            depth_raw = self._depth.get(exchange, {}).get(symbol)
            if depth_raw is not None:
                return copy.deepcopy(depth_raw)
        return None

    async def health_status(self) -> Dict[str, Dict]:
        """Return WS health info per exchange:symbol.

        FIX CRITICAL #7: Now uses _ws_instances instead of stub _get_ws_client().
        """
        result = {}
        for key, task in self._tasks.items():
            ob = None
            parts = key.split(":", 1)
            if len(parts) == 2:
                async with self._lock:
                    ob = self._orderbooks.get(parts[0], {}).get(parts[1])
                    ws_client = self._ws_instances.get(key)
            else:
                ws_client = None

            exchange = parts[0] if len(parts) == 2 else None
            stale = False
            if ob and exchange:
                # FIX #7: Check actual WS instance liveness
                if ws_client and hasattr(ws_client, "is_connected") and not ws_client.is_connected():
                    stale = True
                elif ws_client and hasattr(ws_client, "is_alive") and not ws_client.is_alive():
                    stale = True
                if not stale and (time.time() - ob.timestamp > self._stale_after_sec):
                    stale = True
            result[key] = {
                "alive": not task.done() and not stale,
                "stale": stale,
                "restarts": self._restart_counts.get(key, 0),
                "invalid_updates": self._invalid_update_counts.get(key, 0),
                "last_update_age": round(time.time() - ob.timestamp, 1) if ob else None,
            }
        return result

    def mark_stale_for_reconnect(self, exchange: str, symbol: str) -> None:
        """Force reconnect for a stale WS feed detected by external watchdog."""
        key = f"{exchange}:{symbol}"
        task = self._tasks.get(key)
        if task and not task.done():
            task.cancel()
            logger.warning("ws_orderbooks: cancelled stale task %s for reconnect", key)
