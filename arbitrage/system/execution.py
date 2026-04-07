from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple

from arbitrage.system.config import ExecutionConfig
from arbitrage.system.interfaces import ExecutionVenue, MonitoringSink
from arbitrage.system.models import ExecutionReport, OpenPosition, TradeIntent
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState

logger = logging.getLogger("trading_system")

# ─── Audit FIX: Centralized constants (was scattered magic numbers) ───
_MIN_ORDERBOOK_MAX_AGE_SECONDS = float(os.getenv("MAX_ORDERBOOK_AGE_SECONDS", "5.0"))
_MIN_NOTIONAL_FALLBACK_USD = float(os.getenv("MIN_NOTIONAL_FALLBACK_USD", "500.0"))
_HEDGE_FILL_THRESHOLD = float(os.getenv("HEDGE_FILL_THRESHOLD", "0.98"))
_HEDGE_RETRIES_DEFAULT = int(os.getenv("HEDGE_RETRIES", "3"))
_ORDER_TIMEOUT_MULTIPLIER = int(os.getenv("ORDER_TIMEOUT_MULTIPLIER", "4"))


@dataclass
class AtomicExecutionEngine:
    config: ExecutionConfig
    venue: ExecutionVenue
    slippage: SlippageModel
    state: SystemState
    monitor: MonitoringSink

    def __post_init__(self) -> None:
        # FIX CRITICAL #3: Lazy lock creation bound to active event loop.
        # asyncio.Lock() in __init__ can bind to wrong event loop.
        self._lock: asyncio.Lock | None = None
        self._symbol_locks: Dict[str, asyncio.Lock] = {}
        self._exchange_locks: Dict[str, asyncio.Lock] = {}

    async def _ensure_lock(self) -> asyncio.Lock:
        """FIX CRITICAL #3: Lazy lock creation for event-loop safety."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        # FIX CRITICAL #1: dict.setdefault is atomic in CPython (GIL),
        # preventing race condition where two tasks create different Locks.
        if symbol not in self._symbol_locks:
            self._symbol_locks.setdefault(symbol, asyncio.Lock())
        return self._symbol_locks[symbol]

    def _get_exchange_lock(self, exchange: str) -> asyncio.Lock:
        """FIX CRITICAL #1: Atomic per-exchange lock creation."""
        if exchange not in self._exchange_locks:
            self._exchange_locks.setdefault(exchange, asyncio.Lock())
        return self._exchange_locks[exchange]

    async def _acquire_exchange_locks(self, ex_a: str, ex_b: str):
        """Acquire two exchange locks in alphabetical order to prevent ABBA deadlock."""
        first, second = (ex_a, ex_b) if ex_a < ex_b else (ex_b, ex_a)
        lock_first = self._get_exchange_lock(first)
        lock_second = self._get_exchange_lock(second)
        await lock_first.acquire()
        await lock_second.acquire()

    async def _release_exchange_locks(self, ex_a: str, ex_b: str):
        """Release two exchange locks."""
        first, second = (ex_a, ex_b) if ex_a < ex_b else (ex_b, ex_a)
        lock_first = self._get_exchange_lock(first)
        lock_second = self._get_exchange_lock(second)
        lock_second.release()
        lock_first.release()

    # Backward-compat aliases for any code still using the old typo'd names
    _acquire_exchange_lockes = _acquire_exchange_locks
    _release_exchange_lockes = _release_exchange_locks

    @staticmethod
    def _determine_leg_order(intent: TradeIntent) -> tuple[str, str, str, str]:
        reliability_rank = {"okx": 0, "bybit": 1, "htx": 2, "binance": 3}
        exchanges = [intent.long_exchange, intent.short_exchange]
        first_leg = min(exchanges, key=lambda ex: reliability_rank.get(ex, 99))
        second_leg = intent.short_exchange if first_leg == intent.long_exchange else intent.long_exchange
        first_side = "buy" if first_leg == intent.long_exchange else "sell"
        second_side = "buy" if second_leg == intent.long_exchange else "sell"
        return first_leg, first_side, second_leg, second_side

    @staticmethod
    def _determine_exit_leg_order(
        position: OpenPosition,
        long_side: str, short_side: str,
        long_size: float, short_size: float,
    ) -> tuple[str, str, float, str, str, float]:
        reliability_rank = {"okx": 0, "bybit": 1, "htx": 2, "binance": 3}
        candidates = [
            (position.long_exchange, long_side, long_size),
            (position.short_exchange, short_side, short_size),
        ]
        first = min(candidates, key=lambda x: reliability_rank.get(x[0], 99))
        second = candidates[1] if candidates[0] == first else candidates[0]
        return first[0], first[1], first[2], second[0], second[1], second[2]

    async def execute_dual_entry(
        self, intent: TradeIntent, notional_usd: float,
        est_book_depth_usd: float, volatility: float,
        latency_ms: float, order_type: str = "ioc",
    ) -> ExecutionReport:
        async with self._get_symbol_lock(intent.symbol):
            t0 = asyncio.get_running_loop().time()
            slippage_bps = self.slippage.estimate(notional_usd, est_book_depth_usd, volatility, latency_ms)
            if self.config.dry_run:
                return await self._open_dry_position(intent, notional_usd, slippage_bps)
            try:
                if hasattr(self.venue, "invalidate_balance_cache"):
                    self.venue.invalidate_balance_cache()
                balances_before_entry = await self._safe_get_balances()
                if intent.long_exchange == intent.short_exchange:
                    first_leg, first_side = intent.long_exchange, "buy"
                    second_leg, second_side = intent.short_exchange, "sell"
                else:
                    first_leg, first_side, second_leg, second_side = self._determine_leg_order(intent)
                if balances_before_entry:
                    for check_ex in [first_leg, second_leg]:
                        avail = max(0.0, balances_before_entry.get(check_ex, 0.0))
                        buf_pct = getattr(self.venue, "safety_buffer_pct", 0.05)
                        reserve = getattr(self.venue, "safety_reserve_usd", 0.50)
                        max_safe = max(0.0, avail * (1.0 - buf_pct) - reserve)
                        min_notional = 1.0
                        if hasattr(self.venue, "_min_notional_usd"):
                            min_notional = self.venue._min_notional_usd(check_ex, intent.symbol)
                        if avail < min_notional or max_safe < min_notional:
                            await self.monitor.emit("execution_reject", {
                                "leg": 0,
                                "reason": f"preflight_margin_check: {check_ex} available={avail:.2f} min_notional={min_notional:.2f} max_safe={max_safe:.2f}",
                            })
                            return ExecutionReport(success=False, position_id=None,
                                                   fill_price_long=0.0, fill_price_short=0.0,
                                                   notional_usd=notional_usd, slippage_bps=slippage_bps,
                                                   message="first_leg_failed")
                if hasattr(self.venue, "open_contracts"):
                    tracked_positions = await self.state.list_positions()
                    tracked_pairs: Set[Tuple[str, str]] = set()
                    for tp in tracked_positions:
                        tracked_pairs.add((tp.long_exchange, tp.symbol))
                        tracked_pairs.add((tp.short_exchange, tp.symbol))
                    for check_ex in [first_leg, second_leg]:
                        try:
                            contracts = await self.venue.open_contracts(check_ex, intent.symbol)
                            if contracts > 0 and (check_ex, intent.symbol) not in tracked_pairs:
                                logger.warning("[PREFLIGHT_ORPHAN] %s has %.4f untracked contracts on %s",
                                               check_ex, contracts, intent.symbol)
                                await self.monitor.emit("execution_reject", {
                                    "leg": 0, "reason": f"preflight_orphan: {check_ex} {contracts:.4f}",
                                })
                                return ExecutionReport(success=False, position_id=None,
                                                       fill_price_long=0.0, fill_price_short=0.0,
                                                       notional_usd=notional_usd, slippage_bps=slippage_bps,
                                                       message=f"preflight_orphan: {check_ex}")
                        except Exception:
                            pass

                first_leg_timeout_sec = self.config.order_timeout_ms / 1000 * _ORDER_TIMEOUT_MULTIPLIER
                try:
                    first_result = await asyncio.wait_for(
                        self.venue.place_order(first_leg, intent.symbol, first_side, notional_usd, order_type),
                        timeout=first_leg_timeout_sec,
                    )
                except asyncio.TimeoutError:
                    await self.monitor.emit("execution_reject", {"leg": 1, "reason": "first_leg_timeout"})
                    return ExecutionReport(success=False, position_id=None,
                                           fill_price_long=0.0, fill_price_short=0.0,
                                           notional_usd=notional_usd, slippage_bps=slippage_bps,
                                           message="first_leg_failed")

                if not first_result.get("success"):
                    await self.monitor.emit("execution_reject", {"leg": 1, "reason": first_result.get("message", "first_leg_failed")})
                    return ExecutionReport(success=False, position_id=None,
                                           fill_price_long=0.0, fill_price_short=0.0,
                                           notional_usd=notional_usd, slippage_bps=slippage_bps,
                                           message="first_leg_failed")

                first_order_id = str(first_result.get("order_id") or "")
                if first_order_id:
                    filled = await self.venue.wait_for_fill(
                        first_leg, intent.symbol, first_order_id,
                        self.config.order_timeout_ms,
                        expected_size=float(first_result.get("size", 0.0) or 0.0) or None,
                    )
                    if not filled:
                        if hasattr(self.venue, "cancel_order"):
                            await self.venue.cancel_order(first_leg, first_order_id, intent.symbol)
                        await self.monitor.emit("execution_reject", {"leg": 1, "reason": "first_leg_not_filled"})
                        return ExecutionReport(success=False, position_id=None,
                                               fill_price_long=0.0, fill_price_short=0.0,
                                               notional_usd=notional_usd, slippage_bps=slippage_bps,
                                               message="first_leg_not_filled")

                first_effective = float(first_result.get("effective_notional", notional_usd) or notional_usd)
                second_notional = min(notional_usd, first_effective)
                max_second_leg_attempts = 3

                for attempt in range(max_second_leg_attempts):
                    current_order_type = order_type if attempt == 0 else "market"
                    second_result = await self.venue.place_order(
                        second_leg, intent.symbol, second_side, second_notional, current_order_type,
                    )
                    if second_result.get("success"):
                        second_order_id = str(second_result.get("order_id") or "")
                        if second_order_id:
                            second_filled = await self.venue.wait_for_fill(
                                second_leg, intent.symbol, second_order_id,
                                self.config.order_timeout_ms,
                                expected_size=float(second_result.get("size", 0.0) or 0.0) or None,
                            )
                            if second_filled:
                                return await self._open_live_position(
                                    intent, notional_usd, slippage_bps,
                                    {**first_result, "exchange": first_leg},
                                    {**second_result, "exchange": second_leg},
                                    balances_before_entry,
                                )
                            else:
                                if hasattr(self.venue, "cancel_order"):
                                    try:
                                        await self.venue.cancel_order(second_leg, second_order_id, intent.symbol)
                                    except Exception:
                                        pass
                            await asyncio.sleep(0.3 * (attempt + 1))

                filled_size = float(first_result.get("size", 0.0) or 0.0)
                hedge_side = "sell" if first_side == "buy" else "buy"
                leg_kinds = dict(intent.metadata.get("leg_kinds") or {})
                hedged = False
                hedge_verified = False
                remaining_contracts = None

                if filled_size > 0:
                    async def _check_open(ex: str) -> float:
                        if hasattr(self.venue, "open_contracts"):
                            try:
                                return await self.venue.open_contracts(ex, intent.symbol)
                            except Exception:
                                return -1.0
                        return -1.0

                    try:
                        hedged, hedge_verified, remaining_contracts = await asyncio.wait_for(
                            self._hedge_first_leg(
                                first_leg, intent.symbol, hedge_side, notional_usd,
                                filled_size, _check_open, leg_kinds,
                            ),
                            timeout=self.config.hedge_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        await self.monitor.emit("execution_hedge_timeout", {
                            "symbol": intent.symbol, "exchange": first_leg,
                            "timeout_sec": self.config.hedge_timeout_seconds,
                        })

                await self.monitor.emit("execution_hedge", {
                    "position_symbol": intent.symbol, "hedged": hedged,
                    "first_leg_exchange": first_leg, "verified": hedge_verified,
                    "remaining_contracts": remaining_contracts,
                })
                return ExecutionReport(success=False, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=notional_usd, slippage_bps=slippage_bps,
                                       message="second_leg_failed", hedged=hedged)
            except asyncio.TimeoutError:
                await self.monitor.emit("execution_reject", {"leg": 0, "reason": "order_timeout"})
                return ExecutionReport(success=False, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=notional_usd, slippage_bps=slippage_bps,
                                       message="order_timeout")
            except Exception as exc:
                await self.monitor.emit("execution_reject", {"leg": 0, "reason": f"execution_error:{exc}"})
                return ExecutionReport(success=False, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=notional_usd, slippage_bps=slippage_bps,
                                       message="execution_error")
            finally:
                elapsed_ms = (asyncio.get_running_loop().time() - t0) * 1000
                await self.monitor.emit("execution_latency", {
                    "symbol": intent.symbol, "strategy": intent.strategy_id.value,
                    "ms": round(elapsed_ms, 2),
                })

    async def execute_dual_exit(self, position: OpenPosition, reason: str, order_type: str = "ioc") -> bool:
        async with self._get_symbol_lock(position.symbol):
            leg_kinds = dict(position.metadata.get("leg_kinds") or {})
            long_side, short_side = "sell", "buy"
            long_size = float(position.metadata.get(f"size_{position.long_exchange}", 0.0) or 0.0)
            short_size = float(position.metadata.get(f"size_{position.short_exchange}", 0.0) or 0.0)
            first_leg, first_side, first_size, second_leg, second_side, second_size = self._determine_exit_leg_order(
                position, long_side, short_side, long_size, short_size)
            if leg_kinds.get(first_leg) == "spot":
                first = await self.venue.place_spot_order(first_leg, position.symbol, first_side, first_size, order_type)
            else:
                first = await self.venue.place_order(first_leg, position.symbol, first_side,
                                                     position.notional_usd, order_type,
                                                     quantity_contracts=first_size if first_size > 0 else None, offset="close")
            if not first.get("success"):
                await self.monitor.emit("execution_exit_reject", {"leg": 1,
                    "symbol": position.symbol, "position_id": position.position_id,
                    "reason": first.get("message", "unknown")})
                return False
            if leg_kinds.get(second_leg) == "spot":
                second = await self.venue.place_spot_order(second_leg, position.symbol, second_side, second_size, order_type)
            else:
                second = await self.venue.place_order(second_leg, position.symbol, second_side,
                                                      position.notional_usd, order_type,
                                                      quantity_contracts=second_size if second_size > 0 else None, offset="close")
            if second.get("success"):
                await self.monitor.emit("execution_exit_fill", {
                    "symbol": position.symbol, "position_id": position.position_id,
                    "reason": reason, "first_leg_exchange": first_leg,
                    "second_leg_exchange": second_leg,
                })
                return True
            await self.monitor.emit("execution_exit_reject", {"leg": 2,
                "symbol": position.symbol, "position_id": position.position_id,
                "reason": second.get("message", "unknown"), "exchange": second_leg})
            restore_side = "buy" if first_side == "sell" else "sell"
            restored = False
            for _ in range(self.config.hedge_retries):
                if leg_kinds.get(first_leg) == "spot":
                    restore = await self.venue.place_spot_order(first_leg, position.symbol, restore_side, first_size, "ioc")
                else:
                    restore = await self.venue.place_order(first_leg, position.symbol, restore_side,
                                                           position.notional_usd, "ioc",
                                                           quantity_contracts=first_size if first_size > 0 else None, offset="open")
                if restore.get("success"):
                    restored = True
                    break
                await asyncio.sleep(self.config.order_timeout_ms / 1000)
            await self.monitor.emit("execution_exit_recover", {
                "symbol": position.symbol, "position_id": position.position_id,
                "restored": restored, "restore_exchange": first_leg,
            })
            return False

    async def execute_multi_leg_spot(self, intent: TradeIntent, *, order_type: str = "ioc") -> ExecutionReport:
        legs = intent.metadata.get("legs") or []
        if not legs or len(legs) < 3:
            return ExecutionReport(success=False, position_id=None,
                                   fill_price_long=0.0, fill_price_short=0.0,
                                   notional_usd=0.0, slippage_bps=0.0, message="invalid_legs")
        atomic_mode = str(intent.metadata.get("atomic_mode", "") or "").lower()
        if atomic_mode == "rfq":
            if self.config.dry_run:
                await self.monitor.emit("execution_fill", {"dry_run": True, "symbol": intent.symbol,
                    "strategy": intent.strategy_id.value, "rfq": True})
                return ExecutionReport(success=True, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=0.0, slippage_bps=0.0, message="rfq_dry_run")
            payload = intent.metadata.get("rfq_payload")
            exchange = str(intent.metadata.get("exchange") or intent.long_exchange or "").lower()
            if not payload or not exchange:
                return ExecutionReport(success=False, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=0.0, slippage_bps=0.0, message="rfq_payload_missing")
            if not hasattr(self.venue, "place_rfq"):
                return ExecutionReport(success=False, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=0.0, slippage_bps=0.0, message="rfq_not_supported")
            resp = await self.venue.place_rfq(exchange, payload)
            if resp.get("success"):
                await self.monitor.emit("execution_fill", {"dry_run": False, "symbol": intent.symbol,
                    "strategy": intent.strategy_id.value, "rfq": True})
                return ExecutionReport(success=True, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=0.0, slippage_bps=0.0, message="rfq_filled")
            await self.monitor.emit("execution_reject", {"reason": resp.get("message", "rfq_failed")})
            return ExecutionReport(success=False, position_id=None,
                                   fill_price_long=0.0, fill_price_short=0.0,
                                   notional_usd=0.0, slippage_bps=0.0, message="rfq_failed")
        async with self._get_symbol_lock(intent.symbol):
            if self.config.dry_run:
                await self.monitor.emit("execution_fill", {"dry_run": True, "symbol": intent.symbol,
                    "strategy": intent.strategy_id.value, "multi_leg": True})
                return ExecutionReport(success=True, position_id=None,
                                       fill_price_long=0.0, fill_price_short=0.0,
                                       notional_usd=0.0, slippage_bps=0.0, message="multi_leg_dry_run")
            executed: List[Dict] = []
            for idx, leg in enumerate(legs, start=1):
                exchange, symbol, side = leg["exchange"], leg["symbol"], leg["side"]
                qty = float(leg.get("quantity_base", 0.0) or 0.0)
                if qty <= 0:
                    await self.monitor.emit("execution_reject", {"leg": idx, "reason": "invalid_quantity"})
                    await self._unwind_spot_legs(executed)
                    return ExecutionReport(success=False, position_id=None,
                                           fill_price_long=0.0, fill_price_short=0.0,
                                           notional_usd=0.0, slippage_bps=0.0, message="invalid_leg_quantity")
                limit_px = float(leg.get("limit_price", 0.0) or 0.0)
                if limit_px > 0 and hasattr(self.venue, "market_data"):
                    try:
                        depth = await self.venue.market_data.fetch_spot_orderbook_depth(exchange, symbol, levels=1)
                        bids = depth.get("bids") or []
                        asks = depth.get("asks") or []
                        if side == "buy" and asks and float(asks[0][0]) > limit_px:
                            await self.monitor.emit("execution_reject", {"leg": idx, "reason": "limit_price_worse"})
                            await self._unwind_spot_legs(executed)
                            return ExecutionReport(success=False, position_id=None,
                                                   fill_price_long=0.0, fill_price_short=0.0,
                                                   notional_usd=0.0, slippage_bps=0.0, message="multi_leg_failed")
                        if side == "sell" and bids and float(bids[0][0]) < limit_px:
                            await self.monitor.emit("execution_reject", {"leg": idx, "reason": "limit_price_worse"})
                            await self._unwind_spot_legs(executed)
                            return ExecutionReport(success=False, position_id=None,
                                                   fill_price_long=0.0, fill_price_short=0.0,
                                                   notional_usd=0.0, slippage_bps=0.0, message="multi_leg_failed")
                    except Exception:
                        pass
                response = await self.venue.place_spot_order(exchange, symbol, side, qty, order_type, limit_px)
                if not response.get("success"):
                    await self.monitor.emit("execution_reject", {"leg": idx,
                        "reason": response.get("message", "unknown"), "exchange": exchange})
                    await self._unwind_spot_legs(executed)
                    return ExecutionReport(success=False, position_id=None,
                                           fill_price_long=0.0, fill_price_short=0.0,
                                           notional_usd=0.0, slippage_bps=0.0, message="multi_leg_failed")
                order_id = str(response.get("order_id") or "")
                if not await self.venue.wait_for_fill(exchange, symbol, order_id,
                        self.config.order_timeout_ms, spot=True,
                        expected_size=float(response.get("size", 0.0) or 0.0) or None):
                    await self.monitor.emit("execution_reject", {"leg": idx, "reason": "leg_not_filled"})
                    await self._unwind_spot_legs(executed)
                    return ExecutionReport(success=False, position_id=None,
                                           fill_price_long=0.0, fill_price_short=0.0,
                                           notional_usd=0.0, slippage_bps=0.0, message="multi_leg_failed")
                executed.append({"exchange": exchange, "symbol": symbol, "side": side, "quantity_base": qty})
            await self.monitor.emit("execution_fill", {"dry_run": False, "symbol": intent.symbol,
                "strategy": intent.strategy_id.value, "multi_leg": True})
            return ExecutionReport(success=True, position_id=None,
                                   fill_price_long=0.0, fill_price_short=0.0,
                                   notional_usd=0.0, slippage_bps=0.0, message="multi_leg_filled")

    async def _unwind_spot_legs(self, executed: List[Dict]) -> None:
        for leg in reversed(executed):
            try:
                side = "sell" if leg["side"] == "buy" else "buy"
                await self.venue.place_spot_order(
                    leg["exchange"], leg["symbol"], side,
                    float(leg.get("quantity_base", 0.0) or 0.0), "ioc", 0.0)
            except Exception as exc:
                logger.warning("unwind_spot_leg_failed: %s %s: %s", leg.get("exchange"), leg.get("symbol"), exc)
                continue

    async def _open_dry_position(self, intent: TradeIntent, notional_usd: float, slippage_bps: float) -> ExecutionReport:
        position_id = str(uuid.uuid4())
        await self.state.add_position(OpenPosition(position_id=position_id, strategy_id=intent.strategy_id,
            symbol=intent.symbol, long_exchange=intent.long_exchange, short_exchange=intent.short_exchange,
            notional_usd=notional_usd, entry_mid=float(intent.metadata.get("entry_mid", 0.0)),
            stop_loss_bps=intent.stop_loss_bps, metadata=dict(intent.metadata)))
        await self.monitor.emit("execution_fill", {"dry_run": True, "symbol": intent.symbol,
            "notional": notional_usd, "strategy": intent.strategy_id.value})
        return ExecutionReport(success=True, position_id=position_id,
                               fill_price_long=float(intent.metadata.get("long_price", 0.0)),
                               fill_price_short=float(intent.metadata.get("short_price", 0.0)),
                               notional_usd=notional_usd, slippage_bps=slippage_bps, message="dry_run_fill")

    async def _open_live_position(self, intent: TradeIntent, notional_usd: float, slippage_bps: float,
            first: Dict, second: Dict, balances_before_entry: Dict[str, float] | None = None) -> ExecutionReport:
        position_id = str(uuid.uuid4())
        fill_long = first["fill_price"] if first["exchange"] == intent.long_exchange else second["fill_price"]
        fill_short = first["fill_price"] if first["exchange"] == intent.short_exchange else second["fill_price"]
        actual_notional = min(float(first.get("effective_notional", notional_usd)),
                              float(second.get("effective_notional", notional_usd)))
        await self.state.add_position(OpenPosition(position_id=position_id,
            strategy_id=intent.strategy_id, symbol=intent.symbol,
            long_exchange=intent.long_exchange, short_exchange=intent.short_exchange,
            notional_usd=actual_notional,
            entry_mid=(fill_long + fill_short) / 2, stop_loss_bps=intent.stop_loss_bps,
            metadata={
                **dict(intent.metadata),
                "entry_long_price": float(fill_long), "entry_short_price": float(fill_short),
                f"size_{first['exchange']}": float(first.get("size", 0.0) or 0.0),
                f"size_{second['exchange']}": float(second.get("size", 0.0) or 0.0),
                "notional_leg_first": float(first.get("effective_notional", notional_usd)),
                "notional_leg_second": float(second.get("effective_notional", notional_usd)),
                f"balance_entry_{intent.long_exchange}": float(
                    (balances_before_entry or {}).get(intent.long_exchange, 0.0) or 0.0),
                f"balance_entry_{intent.short_exchange}": float(
                    (balances_before_entry or {}).get(intent.short_exchange, 0.0) or 0.0),
            }))
        await self.monitor.emit("execution_fill", {"dry_run": False, "symbol": intent.symbol,
            "position_id": position_id, "strategy": intent.strategy_id.value,
            "long_exchange": intent.long_exchange, "short_exchange": intent.short_exchange,
            "entry_long_price": float(fill_long), "entry_short_price": float(fill_short)})
        return ExecutionReport(success=True, position_id=position_id,
                               fill_price_long=fill_long, fill_price_short=fill_short,
                               notional_usd=actual_notional, slippage_bps=slippage_bps, message="filled")

    async def _place_maker_leg(self, exchange: str, symbol: str, side: str,
                                notional_usd: float, reference_price: float) -> dict:
        offset_mult = self.config.maker_price_offset_bps / 10_000
        maker_price = reference_price * (1 - offset_mult) if side == "buy" else reference_price * (1 + offset_mult)
        for attempt in range(self.config.maker_max_retries + 1):
            result = await self.venue.place_order(exchange, symbol, side, notional_usd, "post_only", maker_price)
            if not result.get("success"):
                logger.info("[MAKER_REJECTED] %s %s on %s attempt=%d — fallback taker",
                            side, symbol, exchange, attempt + 1)
                return await self.venue.place_order(exchange, symbol, side, notional_usd, "ioc", reference_price)
            order_id = str(result.get("order_id") or "")
            if not order_id:
                return result
            filled = await self.venue.wait_for_fill(exchange, symbol, order_id,
                self.config.maker_timeout_ms, expected_size=float(result.get("size", 0.0) or 0.0) or None)
            if filled:
                logger.info("[MAKER_FILLED] %s %s on %s attempt=%d", side, symbol, exchange, attempt + 1)
                return result
            if hasattr(self.venue, "cancel_order"):
                await self.venue.cancel_order(exchange, order_id, symbol)
            if attempt < self.config.maker_max_retries:
                adjustment = offset_mult * (attempt + 2)
                maker_price = reference_price * (1 - adjustment / 2) if side == "buy" else reference_price * (1 + adjustment / 2)
        logger.info("[MAKER_FALLBACK] %s %s on %s — all attempts failed, using taker",
                    side, symbol, exchange)
        return await self.venue.place_order(exchange, symbol, side, notional_usd, "ioc", reference_price)

    async def _hedge_first_leg(self, first_leg: str, symbol: str, hedge_side: str,
                                notional_usd: float, first_size: float,
                                _spot_qty, leg_kinds: dict) -> tuple[bool, bool, float | None]:
        remaining_contracts = None
        for _ in range(self.config.hedge_retries):
            if leg_kinds.get(first_leg) == "spot":
                unwind = await self.venue.place_spot_order(first_leg, symbol, hedge_side,
                    first_size if first_size > 0 else _spot_qty(first_leg), "ioc")
            else:
                unwind = await self.venue.place_order(first_leg, symbol, hedge_side,
                    notional_usd, "ioc", quantity_contracts=first_size if first_size > 0 else None,
                    offset="close")
            if unwind.get("success"):
                await asyncio.sleep(self.config.hedge_settle_seconds)
                hedge_verified = False
                if hasattr(self.venue, "open_contracts"):
                    try:
                        remaining_contracts = await self.venue.open_contracts(first_leg, symbol)
                        # Audit FIX #4: Verify hedge actually closes position
                        if remaining_contracts <= 0:
                            return True, True, remaining_contracts
                    except Exception as exc:
                        logger.warning("hedge_verify_failed: %s %s: %s", first_leg, symbol, exc)
                        remaining_contracts = None
                        hedge_verified = False
                    return True, hedge_verified, remaining_contracts
            await asyncio.sleep(self.config.order_timeout_ms / 1000)
        return False, False, remaining_contracts

    async def _safe_get_balances(self) -> Dict[str, float]:
        try:
            return await self.venue.get_balances()
        except Exception as exc:
            logging.getLogger("trading_system").warning("balance_fetch_failed: %s", exc)
            return {}
