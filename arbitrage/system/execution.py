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
        self._symbol_locks: Dict[str, asyncio.Lock] = {}

    def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._symbol_locks:
            self._symbol_locks[symbol] = asyncio.Lock()
        return self._symbol_locks[symbol]

    async def execute_dual_entry(
        self,
        intent: TradeIntent,
        notional_usd: float,
        est_book_depth_usd: float,
        volatility: float,
        latency_ms: float,
        order_type: str = "ioc",
    ) -> ExecutionReport:
        async with self._get_symbol_lock(intent.symbol):
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
                filled_size = float((filled_result or {}).get("size", 0.0) or 0.0)
                try:
                    hedged, hedge_verified, remaining_contracts = await asyncio.wait_for(
                        self._hedge_first_leg(
                            filled_leg, intent.symbol, hedge_side, notional_usd,
                            filled_size, _spot_qty, leg_kinds,
                        ),
                        timeout=self.config.hedge_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    await self.monitor.emit(
                        "execution_hedge_timeout",
                        {"symbol": intent.symbol, "exchange": filled_leg, "timeout_sec": self.config.hedge_timeout_seconds},
                    )

                await self.monitor.emit(
                    "execution_hedge",
                    {
                        "position_symbol": intent.symbol,
                        "hedged": hedged,
                        "first_leg_exchange": filled_leg,
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
        async with self._get_symbol_lock(position.symbol):
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
        async with self._get_symbol_lock(intent.symbol):
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
            except Exception as exc:
                logger.warning("unwind_spot_leg_failed: %s %s: %s", leg.get("exchange"), leg.get("symbol"), exc)
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

    async def _place_maker_leg(
        self,
        exchange: str,
        symbol: str,
        side: str,
        notional_usd: float,
        reference_price: float,
    ) -> dict:
        """Place a post-only maker order with retry and cancel/replace logic.

        The maker order is placed slightly inside the spread (by maker_price_offset_bps)
        to increase fill probability while keeping post-only status.
        If it doesn't fill within maker_timeout_ms, cancel and re-place up to
        maker_max_retries times.  If all retries fail, fall back to a taker IOC.

        Returns the same dict format as venue.place_order().
        """
        offset_mult = self.config.maker_price_offset_bps / 10_000
        # Nudge maker price towards the spread to increase fill chance
        if side == "buy":
            # Buy maker: place slightly above current best bid (but below ask)
            maker_price = reference_price * (1 - offset_mult)
        else:
            # Sell maker: place slightly below current best ask (but above bid)
            maker_price = reference_price * (1 + offset_mult)

        for attempt in range(self.config.maker_max_retries + 1):
            result = await self.venue.place_order(
                exchange, symbol, side, notional_usd,
                "post_only", maker_price,
            )
            if not result.get("success"):
                # Post-only rejected (would have crossed) — fall back to taker
                logger.info(
                    "[MAKER_REJECTED] %s %s on %s attempt=%d — falling back to taker",
                    side, symbol, exchange, attempt + 1,
                )
                return await self.venue.place_order(
                    exchange, symbol, side, notional_usd, "ioc", reference_price,
                )

            order_id = str(result.get("order_id") or "")
            if not order_id:
                return result  # Can't track — return as-is

            # Wait for fill with maker-specific timeout
            filled = await self.venue.wait_for_fill(
                exchange, symbol, order_id, self.config.maker_timeout_ms,
                expected_size=float(result.get("size", 0.0) or 0.0) or None,
            )
            if filled:
                logger.info(
                    "[MAKER_FILLED] %s %s on %s attempt=%d — saved taker fees",
                    side, symbol, exchange, attempt + 1,
                )
                return result

            # Not filled — cancel and retry or fall back
            if hasattr(self.venue, "cancel_order"):
                await self.venue.cancel_order(exchange, order_id, symbol)

            if attempt < self.config.maker_max_retries:
                logger.info(
                    "[MAKER_RETRY] %s %s on %s attempt=%d/%d — cancelling and re-placing",
                    side, symbol, exchange, attempt + 1, self.config.maker_max_retries + 1,
                )
                # Adjust price slightly more aggressively on each retry
                adjustment = offset_mult * (attempt + 2)
                if side == "buy":
                    maker_price = reference_price * (1 - adjustment / 2)
                else:
                    maker_price = reference_price * (1 + adjustment / 2)

        # All maker attempts exhausted — fall back to taker IOC
        logger.info(
            "[MAKER_FALLBACK] %s %s on %s — all %d maker attempts failed, using taker",
            side, symbol, exchange, self.config.maker_max_retries + 1,
        )
        return await self.venue.place_order(
            exchange, symbol, side, notional_usd, "ioc", reference_price,
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
                    except Exception as exc:
                        logger.warning("hedge_verify_failed: %s %s: %s", first_leg, symbol, exc)
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
