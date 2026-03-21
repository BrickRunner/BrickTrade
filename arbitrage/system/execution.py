from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Dict

from arbitrage.system.config import ExecutionConfig
from arbitrage.system.interfaces import ExecutionVenue, MonitoringSink
from arbitrage.system.models import ExecutionReport, OpenPosition, TradeIntent
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.state import SystemState

logger = logging.getLogger("trading_system")


@dataclass
class AtomicExecutionEngine:
    config: ExecutionConfig
    venue: ExecutionVenue
    slippage: SlippageModel
    state: SystemState
    monitor: MonitoringSink

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def execute_dual_entry(
        self,
        intent: TradeIntent,
        notional_usd: float,
        est_book_depth_usd: float,
        volatility: float,
        latency_ms: float,
        order_type: str = "ioc",
    ) -> ExecutionReport:
        async with self._lock:
            t0 = asyncio.get_event_loop().time()
            slippage_bps = self.slippage.estimate(notional_usd, est_book_depth_usd, volatility, latency_ms)
            if self.config.dry_run:
                return await self._open_dry_position(intent, notional_usd, slippage_bps)
            try:
                # Invalidate cache to get FRESH balances for preflight check
                if hasattr(self.venue, "invalidate_balance_cache"):
                    self.venue.invalidate_balance_cache()
                balances_before_entry = await self._safe_get_balances()
                if intent.long_exchange == intent.short_exchange:
                    first_leg = intent.long_exchange
                    second_leg = intent.short_exchange
                    first_side = "buy"
                    second_side = "sell"
                else:
                    first_leg, first_side, second_leg, second_side = self._determine_leg_order(intent)
                # Pre-flight: verify BOTH exchanges have sufficient margin BEFORE
                # placing any orders.  This prevents the costly scenario where leg-1
                # fills, leg-2 is rejected for insufficient margin, and we have to
                # hedge leg-1 back — paying fees for a zero-sum round-trip.
                if balances_before_entry:
                    for check_ex in [first_leg, second_leg]:
                        avail = max(0.0, balances_before_entry.get(check_ex, 0.0))
                        # Use the venue's safety parameters if available
                        buf_pct = getattr(self.venue, "safety_buffer_pct", 0.05)
                        reserve = getattr(self.venue, "safety_reserve_usd", 0.50)
                        max_safe = max(0.0, avail * (1.0 - buf_pct) - reserve)
                        # Check against min notional for this exchange
                        min_notional = 1.0
                        if hasattr(self.venue, "_min_notional_usd"):
                            min_notional = self.venue._min_notional_usd(check_ex, intent.symbol)
                        if avail < min_notional or max_safe < min_notional:
                            await self.monitor.emit(
                                "execution_reject",
                                {
                                    "leg": 0,
                                    "reason": f"preflight_margin_check: {check_ex} available={avail:.2f} min_notional={min_notional:.2f} max_safe={max_safe:.2f}",
                                },
                            )
                            return ExecutionReport(
                                success=False,
                                position_id=None,
                                fill_price_long=0.0,
                                fill_price_short=0.0,
                                notional_usd=notional_usd,
                                slippage_bps=slippage_bps,
                                message="first_leg_failed",
                            )

                # Fix #8: Check for orphaned positions on BOTH exchanges.
                # If an exchange has untracked open contracts, its margin is locked
                # and the trade WILL fail — reject before placing any orders.
                if hasattr(self.venue, "open_contracts"):
                    tracked_positions = await self.state.list_positions()
                    tracked_pairs: set[tuple[str, str]] = set()
                    for tp in tracked_positions:
                        tracked_pairs.add((tp.long_exchange, tp.symbol))
                        tracked_pairs.add((tp.short_exchange, tp.symbol))
                    for check_ex in [first_leg, second_leg]:
                        try:
                            contracts = await self.venue.open_contracts(check_ex, intent.symbol)
                            if contracts > 0 and (check_ex, intent.symbol) not in tracked_pairs:
                                logger.warning(
                                    "[PREFLIGHT_ORPHAN] %s has %.4f untracked contracts on %s — "
                                    "margin is locked, rejecting trade",
                                    check_ex, contracts, intent.symbol,
                                )
                                await self.monitor.emit(
                                    "execution_reject",
                                    {
                                        "leg": 0,
                                        "reason": f"preflight_orphan_position: {check_ex} has {contracts:.4f} untracked contracts on {intent.symbol}",
                                    },
                                )
                                return ExecutionReport(
                                    success=False,
                                    position_id=None,
                                    fill_price_long=0.0,
                                    fill_price_short=0.0,
                                    notional_usd=notional_usd,
                                    slippage_bps=slippage_bps,
                                    message="orphan_position_blocks_trade",
                                )
                        except Exception:
                            pass  # If check fails, continue — other safeguards still apply

                leg_kinds = dict(intent.metadata.get("leg_kinds") or {})
                raw_limit_prices = dict(intent.metadata.get("limit_prices") or {})
                # Add slippage buffer to IOC limit prices so they have a better
                # chance of filling despite REST latency.  Buy prices are nudged
                # up, sell prices are nudged down by ~15 bps.
                _SLIPPAGE_BUFFER = 0.0015  # 15 bps
                limit_prices: dict = {}
                for side_key, px in raw_limit_prices.items():
                    px = float(px or 0.0)
                    if px <= 0:
                        limit_prices[side_key] = 0.0
                    elif side_key == "buy":
                        limit_prices[side_key] = px * (1 + _SLIPPAGE_BUFFER)
                    else:
                        limit_prices[side_key] = px * (1 - _SLIPPAGE_BUFFER)

                def _spot_qty(exchange: str) -> float:
                    price = float(intent.metadata.get("spot_price", 0.0) or 0.0)
                    if exchange == intent.long_exchange:
                        price = float(intent.metadata.get("long_price", price) or price)
                    if exchange == intent.short_exchange:
                        price = float(intent.metadata.get("short_price", price) or price)
                    if price <= 0:
                        return 0.0
                    return notional_usd / price

                if leg_kinds.get(first_leg) == "spot":
                    qty = _spot_qty(first_leg)
                    if qty <= 0:
                        await self.monitor.emit("execution_reject", {"leg": 1, "reason": "spot_qty_unavailable"})
                        return ExecutionReport(
                            success=False,
                            position_id=None,
                            fill_price_long=0.0,
                            fill_price_short=0.0,
                            notional_usd=notional_usd,
                            slippage_bps=slippage_bps,
                            message="first_leg_failed",
                        )
                    first_limit = float(limit_prices.get(first_side, 0.0) or 0.0)
                    first_order_type = "ioc" if first_limit > 0 else order_type
                    first = await self.venue.place_spot_order(
                        first_leg, intent.symbol, first_side, qty, first_order_type, first_limit
                    )
                else:
                    first_limit = float(limit_prices.get(first_side, 0.0) or 0.0)
                    first_order_type = "ioc" if first_limit > 0 else order_type
                    first = await self.venue.place_order(
                        first_leg, intent.symbol, first_side, notional_usd, first_order_type, first_limit
                    )
                if not first.get("success"):
                    await self.monitor.emit("execution_reject", {"leg": 1, "reason": first.get("message", "unknown")})
                    return ExecutionReport(
                        success=False,
                        position_id=None,
                        fill_price_long=0.0,
                        fill_price_short=0.0,
                        notional_usd=notional_usd,
                        slippage_bps=slippage_bps,
                        message="first_leg_failed",
                    )
                first_order_id = str(first.get("order_id") or "")
                if not await self.venue.wait_for_fill(
                    first_leg,
                    intent.symbol,
                    first_order_id,
                    self.config.order_timeout_ms,
                    spot=leg_kinds.get(first_leg) == "spot",
                    expected_size=float(first.get("size", 0.0) or 0.0) or None,
                ):
                    await self.monitor.emit("execution_reject", {"leg": 1, "reason": "first_leg_not_filled"})
                    return ExecutionReport(
                        success=False,
                        position_id=None,
                        fill_price_long=0.0,
                        fill_price_short=0.0,
                        notional_usd=notional_usd,
                        slippage_bps=slippage_bps,
                        message="first_leg_failed",
                    )

                # Invalidate balance cache so second leg sees updated margin
                if hasattr(self.venue, "invalidate_balance_cache"):
                    self.venue.invalidate_balance_cache()

                # CRITICAL: Second leg MUST fill — if it doesn't, we have to hedge
                # the first leg back at a loss (fees + slippage).  Strategy:
                # 1. Try market order directly (guaranteed fill, fastest)
                # 2. Only hedge if market order fails (exchange down / no liquidity)
                #
                # Why market order for second leg:
                # - First leg already filled, we are COMMITTED
                # - IOC limit can fail if price moved during first leg execution
                # - Market order slippage on small sizes is negligible
                # - Failed second leg + hedge costs MORE than market slippage
                second_filled = False
                second = {}

                if leg_kinds.get(second_leg) == "spot":
                    qty = _spot_qty(second_leg)
                    if qty > 0:
                        second = await self.venue.place_spot_order(
                            second_leg, intent.symbol, second_side, qty, "market", 0.0
                        )
                else:
                    second = await self.venue.place_order(
                        second_leg, intent.symbol, second_side, notional_usd, "market", 0.0
                    )

                if second.get("success"):
                    second_order_id = str(second.get("order_id") or "")
                    if await self.venue.wait_for_fill(
                        second_leg,
                        intent.symbol,
                        second_order_id,
                        self.config.order_timeout_ms,
                        spot=leg_kinds.get(second_leg) == "spot",
                        expected_size=float(second.get("size", 0.0) or 0.0) or None,
                    ):
                        second_filled = True
                    else:
                        # Market order placed but fill not confirmed — check if position
                        # actually exists before hedging to avoid creating orphaned positions.
                        if hasattr(self.venue, "open_contracts"):
                            try:
                                contracts = await self.venue.open_contracts(second_leg, intent.symbol)
                                if contracts > 0:
                                    # Position exists on second leg — treat as filled
                                    second_filled = True
                                    await self.monitor.emit(
                                        "execution_fill_recovery",
                                        {"leg": 2, "exchange": second_leg, "contracts": contracts},
                                    )
                            except Exception:
                                pass
                        if not second_filled:
                            await self.monitor.emit(
                                "execution_reject",
                                {"leg": 2, "reason": "market_order_not_confirmed", "exchange": second_leg},
                            )
                else:
                    await self.monitor.emit(
                        "execution_reject",
                        {"leg": 2, "reason": second.get("message", "unknown"), "exchange": second_leg},
                    )

                if second_filled:
                    return await self._open_live_position(
                        intent,
                        notional_usd,
                        slippage_bps,
                        first,
                        second,
                        balances_before_entry,
                    )

                # Market order failed — must hedge first leg
                await self.monitor.emit(
                    "execution_reject",
                    {"leg": 2, "reason": "second_leg_failed_hedging", "exchange": second_leg},
                )

                # Hedge / unwind if second leg failed — wrapped in timeout.
                hedge_side = "sell" if first_side == "buy" else "buy"
                hedged = False
                hedge_verified = False
                remaining_contracts = None
                first_size = float(first.get("size", 0.0) or 0.0)
                try:
                    hedged, hedge_verified, remaining_contracts = await asyncio.wait_for(
                        self._hedge_first_leg(
                            first_leg, intent.symbol, hedge_side, notional_usd,
                            first_size, _spot_qty, leg_kinds,
                        ),
                        timeout=self.config.hedge_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    await self.monitor.emit(
                        "execution_hedge_timeout",
                        {"symbol": intent.symbol, "exchange": first_leg, "timeout_sec": self.config.hedge_timeout_seconds},
                    )

                await self.monitor.emit(
                    "execution_hedge",
                    {
                        "position_symbol": intent.symbol,
                        "hedged": hedged,
                        "first_leg_exchange": first_leg,
                        "verified": hedge_verified,
                        "remaining_contracts": remaining_contracts,
                    },
                )
                return ExecutionReport(
                    success=False,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=notional_usd,
                    slippage_bps=slippage_bps,
                    message="second_leg_failed",
                    hedged=hedged,
                )
            except asyncio.TimeoutError:
                await self.monitor.emit("execution_reject", {"leg": 0, "reason": "order_timeout"})
                return ExecutionReport(
                    success=False,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=notional_usd,
                    slippage_bps=slippage_bps,
                    message="order_timeout",
                )
            except Exception as exc:
                await self.monitor.emit("execution_reject", {"leg": 0, "reason": f"execution_error:{exc}"})
                return ExecutionReport(
                    success=False,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=notional_usd,
                    slippage_bps=slippage_bps,
                    message="execution_error",
                )
            finally:
                elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
                await self.monitor.emit(
                    "execution_latency",
                    {"symbol": intent.symbol, "strategy": intent.strategy_id.value, "ms": round(elapsed_ms, 2)},
                )

    async def execute_dual_exit(
        self,
        position: OpenPosition,
        reason: str,
        order_type: str = "ioc",
    ) -> bool:
        async with self._lock:
            leg_kinds = dict(position.metadata.get("leg_kinds") or {})
            long_side = "sell"
            short_side = "buy"
            long_size = float(position.metadata.get(f"size_{position.long_exchange}", 0.0) or 0.0)
            short_size = float(position.metadata.get(f"size_{position.short_exchange}", 0.0) or 0.0)

            first_leg, first_side, first_size, second_leg, second_side, second_size = self._determine_exit_leg_order(
                position, long_side, short_side, long_size, short_size
            )
            if leg_kinds.get(first_leg) == "spot":
                first = await self.venue.place_spot_order(
                    first_leg,
                    position.symbol,
                    first_side,
                    first_size,
                    order_type,
                )
            else:
                first = await self.venue.place_order(
                    first_leg,
                    position.symbol,
                    first_side,
                    position.notional_usd,
                    order_type,
                    quantity_contracts=first_size if first_size > 0 else None,
                    offset="close",
                )
            if not first.get("success"):
                await self.monitor.emit(
                    "execution_exit_reject",
                    {"leg": 1, "symbol": position.symbol, "position_id": position.position_id, "reason": first.get("message", "unknown")},
                )
                return False

            if leg_kinds.get(second_leg) == "spot":
                second = await self.venue.place_spot_order(
                    second_leg,
                    position.symbol,
                    second_side,
                    second_size,
                    order_type,
                )
            else:
                second = await self.venue.place_order(
                    second_leg,
                    position.symbol,
                    second_side,
                    position.notional_usd,
                    order_type,
                    quantity_contracts=second_size if second_size > 0 else None,
                    offset="close",
                )
            if second.get("success"):
                await self.monitor.emit(
                    "execution_exit_fill",
                    {
                        "symbol": position.symbol,
                        "position_id": position.position_id,
                        "reason": reason,
                        "first_leg_exchange": first_leg,
                        "second_leg_exchange": second_leg,
                    },
                )
                return True

            await self.monitor.emit(
                "execution_exit_reject",
                {
                    "leg": 2,
                    "symbol": position.symbol,
                    "position_id": position.position_id,
                    "reason": second.get("message", "unknown"),
                    "exchange": second_leg,
                },
            )

            # Try to restore first leg if second close failed, to avoid unhedged partial close.
            restore_side = "buy" if first_side == "sell" else "sell"
            restored = False
            for _ in range(self.config.hedge_retries):
                if leg_kinds.get(first_leg) == "spot":
                    restore = await self.venue.place_spot_order(
                        first_leg,
                        position.symbol,
                        restore_side,
                        first_size,
                        "ioc",
                    )
                else:
                    restore = await self.venue.place_order(
                        first_leg,
                        position.symbol,
                        restore_side,
                        position.notional_usd,
                        "ioc",
                        quantity_contracts=first_size if first_size > 0 else None,
                        offset="open",
                    )
                if restore.get("success"):
                    restored = True
                    break
                await asyncio.sleep(self.config.order_timeout_ms / 1000)

            await self.monitor.emit(
                "execution_exit_recover",
                {
                    "symbol": position.symbol,
                    "position_id": position.position_id,
                    "restored": restored,
                    "restore_exchange": first_leg,
                },
            )
            return False

    async def execute_multi_leg_spot(
        self,
        intent: TradeIntent,
        *,
        order_type: str = "ioc",
    ) -> ExecutionReport:
        legs = intent.metadata.get("legs") or []
        if not legs or len(legs) < 3:
            return ExecutionReport(
                success=False,
                position_id=None,
                fill_price_long=0.0,
                fill_price_short=0.0,
                notional_usd=0.0,
                slippage_bps=0.0,
                message="invalid_legs",
            )
        atomic_mode = str(intent.metadata.get("atomic_mode", "") or "").lower()
        if atomic_mode == "rfq":
            if self.config.dry_run:
                await self.monitor.emit(
                    "execution_fill",
                    {"dry_run": True, "symbol": intent.symbol, "strategy": intent.strategy_id.value, "rfq": True},
                )
                return ExecutionReport(
                    success=True,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=0.0,
                    slippage_bps=0.0,
                    message="rfq_dry_run",
                )
            payload = intent.metadata.get("rfq_payload")
            exchange = str(intent.metadata.get("exchange") or intent.long_exchange or "").lower()
            if not payload or not exchange:
                return ExecutionReport(
                    success=False,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=0.0,
                    slippage_bps=0.0,
                    message="rfq_payload_missing",
                )
            if not hasattr(self.venue, "place_rfq"):
                return ExecutionReport(
                    success=False,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=0.0,
                    slippage_bps=0.0,
                    message="rfq_not_supported",
                )
            resp = await self.venue.place_rfq(exchange, payload)
            if resp.get("success"):
                await self.monitor.emit(
                    "execution_fill",
                    {"dry_run": False, "symbol": intent.symbol, "strategy": intent.strategy_id.value, "rfq": True},
                )
                return ExecutionReport(
                    success=True,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=0.0,
                    slippage_bps=0.0,
                    message="rfq_filled",
                )
            await self.monitor.emit("execution_reject", {"reason": resp.get("message", "rfq_failed")})
            return ExecutionReport(
                success=False,
                position_id=None,
                fill_price_long=0.0,
                fill_price_short=0.0,
                notional_usd=0.0,
                slippage_bps=0.0,
                message="rfq_failed",
            )
        async with self._lock:
            if self.config.dry_run:
                await self.monitor.emit(
                    "execution_fill",
                    {"dry_run": True, "symbol": intent.symbol, "strategy": intent.strategy_id.value, "multi_leg": True},
                )
                return ExecutionReport(
                    success=True,
                    position_id=None,
                    fill_price_long=0.0,
                    fill_price_short=0.0,
                    notional_usd=0.0,
                    slippage_bps=0.0,
                    message="multi_leg_dry_run",
                )
            executed = []
            for idx, leg in enumerate(legs, start=1):
                exchange = leg["exchange"]
                symbol = leg["symbol"]
                side = leg["side"]
                qty = float(leg.get("quantity_base", 0.0) or 0.0)
                if qty <= 0:
                    await self.monitor.emit("execution_reject", {"leg": idx, "reason": "invalid_quantity"})
                    await self._unwind_spot_legs(executed)
                    return ExecutionReport(
                        success=False,
                        position_id=None,
                        fill_price_long=0.0,
                        fill_price_short=0.0,
                        notional_usd=0.0,
                        slippage_bps=0.0,
                        message="invalid_leg_quantity",
                    )
                limit_px = float(leg.get("limit_price", 0.0) or 0.0)
                if limit_px > 0 and hasattr(self.venue, "market_data"):
                    try:
                        depth = await self.venue.market_data.fetch_spot_orderbook_depth(exchange, symbol, levels=1)
                        bids = depth.get("bids") or []
                        asks = depth.get("asks") or []
                        if side == "buy" and asks and float(asks[0][0]) > limit_px:
                            await self.monitor.emit("execution_reject", {"leg": idx, "reason": "limit_price_worse"})
                            await self._unwind_spot_legs(executed)
                            return ExecutionReport(
                                success=False,
                                position_id=None,
                                fill_price_long=0.0,
                                fill_price_short=0.0,
                                notional_usd=0.0,
                                slippage_bps=0.0,
                                message="multi_leg_failed",
                            )
                        if side == "sell" and bids and float(bids[0][0]) < limit_px:
                            await self.monitor.emit("execution_reject", {"leg": idx, "reason": "limit_price_worse"})
                            await self._unwind_spot_legs(executed)
                            return ExecutionReport(
                                success=False,
                                position_id=None,
                                fill_price_long=0.0,
                                fill_price_short=0.0,
                                notional_usd=0.0,
                                slippage_bps=0.0,
                                message="multi_leg_failed",
                            )
                    except Exception:
                        pass
                response = await self.venue.place_spot_order(
                    exchange, symbol, side, qty, order_type, limit_px
                )
                if not response.get("success"):
                    await self.monitor.emit(
                        "execution_reject",
                        {"leg": idx, "reason": response.get("message", "unknown"), "exchange": exchange},
                    )
                    await self._unwind_spot_legs(executed)
                    return ExecutionReport(
                        success=False,
                        position_id=None,
                        fill_price_long=0.0,
                        fill_price_short=0.0,
                        notional_usd=0.0,
                        slippage_bps=0.0,
                        message="multi_leg_failed",
                    )
                order_id = str(response.get("order_id") or "")
                if not await self.venue.wait_for_fill(
                    exchange,
                    symbol,
                    order_id,
                    self.config.order_timeout_ms,
                    spot=True,
                    expected_size=float(response.get("size", 0.0) or 0.0) or None,
                ):
                    await self.monitor.emit("execution_reject", {"leg": idx, "reason": "leg_not_filled"})
                    await self._unwind_spot_legs(executed)
                    return ExecutionReport(
                        success=False,
                        position_id=None,
                        fill_price_long=0.0,
                        fill_price_short=0.0,
                        notional_usd=0.0,
                        slippage_bps=0.0,
                        message="multi_leg_failed",
                    )
                executed.append({"exchange": exchange, "symbol": symbol, "side": side, "quantity_base": qty})
            await self.monitor.emit(
                "execution_fill",
                {"dry_run": False, "symbol": intent.symbol, "strategy": intent.strategy_id.value, "multi_leg": True},
            )
            return ExecutionReport(
                success=True,
                position_id=None,
                fill_price_long=0.0,
                fill_price_short=0.0,
                notional_usd=0.0,
                slippage_bps=0.0,
                message="multi_leg_filled",
            )

    async def _unwind_spot_legs(self, executed: list[Dict]) -> None:
        for leg in reversed(executed):
            try:
                side = "sell" if leg["side"] == "buy" else "buy"
                await self.venue.place_spot_order(
                    leg["exchange"],
                    leg["symbol"],
                    side,
                    float(leg.get("quantity_base", 0.0) or 0.0),
                    "ioc",
                    0.0,
                )
            except Exception:
                continue

    @staticmethod
    def _determine_leg_order(intent: TradeIntent) -> tuple[str, str, str, str]:
        # Reliability preference: OKX first, then Bybit, then HTX.
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
        long_side: str,
        short_side: str,
        long_size: float,
        short_size: float,
    ) -> tuple[str, str, float, str, str, float]:
        # Reliability preference: OKX first, then Bybit, then HTX.
        reliability_rank = {"okx": 0, "bybit": 1, "htx": 2, "binance": 3}
        candidates = [
            (position.long_exchange, long_side, long_size),
            (position.short_exchange, short_side, short_size),
        ]
        first = min(candidates, key=lambda x: reliability_rank.get(x[0], 99))
        second = candidates[1] if candidates[0] == first else candidates[0]
        return first[0], first[1], first[2], second[0], second[1], second[2]

    async def _open_dry_position(
        self,
        intent: TradeIntent,
        notional_usd: float,
        slippage_bps: float,
    ) -> ExecutionReport:
        position_id = str(uuid.uuid4())
        await self.state.add_position(
            OpenPosition(
                position_id=position_id,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                long_exchange=intent.long_exchange,
                short_exchange=intent.short_exchange,
                notional_usd=notional_usd,
                entry_mid=float(intent.metadata.get("entry_mid", 0.0)),
                stop_loss_bps=intent.stop_loss_bps,
                metadata=dict(intent.metadata),
            )
        )
        await self.monitor.emit(
            "execution_fill",
            {"dry_run": True, "symbol": intent.symbol, "notional": notional_usd, "strategy": intent.strategy_id.value},
        )
        return ExecutionReport(
            success=True,
            position_id=position_id,
            fill_price_long=float(intent.metadata.get("long_price", 0.0)),
            fill_price_short=float(intent.metadata.get("short_price", 0.0)),
            notional_usd=notional_usd,
            slippage_bps=slippage_bps,
            message="dry_run_fill",
        )

    async def _open_live_position(
        self,
        intent: TradeIntent,
        notional_usd: float,
        slippage_bps: float,
        first: Dict,
        second: Dict,
        balances_before_entry: Dict[str, float] | None = None,
    ) -> ExecutionReport:
        position_id = str(uuid.uuid4())
        fill_long = first["fill_price"] if first["exchange"] == intent.long_exchange else second["fill_price"]
        fill_short = first["fill_price"] if first["exchange"] == intent.short_exchange else second["fill_price"]
        actual_notional = min(
            float(first.get("effective_notional", notional_usd)),
            float(second.get("effective_notional", notional_usd)),
        )
        await self.state.add_position(
            OpenPosition(
                position_id=position_id,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                long_exchange=intent.long_exchange,
                short_exchange=intent.short_exchange,
                notional_usd=actual_notional,
                entry_mid=(fill_long + fill_short) / 2,
                stop_loss_bps=intent.stop_loss_bps,
                metadata={
                    **dict(intent.metadata),
                    "entry_long_price": float(fill_long),
                    "entry_short_price": float(fill_short),
                    f"size_{first['exchange']}": float(first.get("size", 0.0) or 0.0),
                    f"size_{second['exchange']}": float(second.get("size", 0.0) or 0.0),
                    "notional_leg_first": float(first.get("effective_notional", notional_usd)),
                    "notional_leg_second": float(second.get("effective_notional", notional_usd)),
                    f"balance_entry_{intent.long_exchange}": float(
                        (balances_before_entry or {}).get(intent.long_exchange, 0.0) or 0.0
                    ),
                    f"balance_entry_{intent.short_exchange}": float(
                        (balances_before_entry or {}).get(intent.short_exchange, 0.0) or 0.0
                    ),
                },
            )
        )
        await self.monitor.emit(
            "execution_fill",
            {
                "dry_run": False,
                "symbol": intent.symbol,
                "position_id": position_id,
                "strategy": intent.strategy_id.value,
                "long_exchange": intent.long_exchange,
                "short_exchange": intent.short_exchange,
                "entry_long_price": float(fill_long),
                "entry_short_price": float(fill_short),
            },
        )
        return ExecutionReport(
            success=True,
            position_id=position_id,
            fill_price_long=fill_long,
            fill_price_short=fill_short,
            notional_usd=actual_notional,
            slippage_bps=slippage_bps,
            message="filled",
        )

    async def _hedge_first_leg(
        self,
        first_leg: str,
        symbol: str,
        hedge_side: str,
        notional_usd: float,
        first_size: float,
        _spot_qty,
        leg_kinds: dict,
    ) -> tuple[bool, bool, float | None]:
        """Execute hedge with retries. Returns (hedged, verified, remaining_contracts)."""
        remaining_contracts = None
        for _ in range(self.config.hedge_retries):
            if leg_kinds.get(first_leg) == "spot":
                unwind = await self.venue.place_spot_order(
                    first_leg,
                    symbol,
                    hedge_side,
                    first_size if first_size > 0 else _spot_qty(first_leg),
                    "ioc",
                )
            else:
                unwind = await self.venue.place_order(
                    first_leg,
                    symbol,
                    hedge_side,
                    notional_usd,
                    "ioc",
                    quantity_contracts=first_size if first_size > 0 else None,
                    offset="close",
                )
            if unwind.get("success"):
                await asyncio.sleep(self.config.hedge_settle_seconds)
                hedge_verified = False
                if hasattr(self.venue, "open_contracts"):
                    try:
                        remaining_contracts = await self.venue.open_contracts(first_leg, symbol)
                        hedge_verified = True
                    except Exception:
                        remaining_contracts = None
                        hedge_verified = False
                if hedge_verified and remaining_contracts is not None and remaining_contracts <= 0:
                    return True, True, remaining_contracts
                if not hedge_verified:
                    return True, False, remaining_contracts
            await asyncio.sleep(self.config.order_timeout_ms / 1000)
        return False, False, remaining_contracts

    async def _safe_get_balances(self) -> Dict[str, float]:
        try:
            return await self.venue.get_balances()
        except Exception as exc:
            import logging
            logging.getLogger("trading_system").warning("balance_fetch_failed: %s", exc)
            return {}
