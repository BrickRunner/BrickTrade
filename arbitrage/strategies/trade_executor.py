"""
Atomic Execution Engine for arbitrage trades.

Pipeline:
    Pre-check → Slippage Estimation → Atomic Execution → Position Sync

Leg execution order:
    - If OKX is one leg: execute non-OKX first (risky), then OKX with IOC (safe)
    - If neither is OKX: execute alphabetically first, then second

On failure:
    - First leg fails → no action (nothing opened)
    - Second leg fails → hedge first leg with 3 retries
    - Hedge fails → create partial position for auto-retry on next cycle
"""
import asyncio
from typing import Dict, Any, Tuple

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.market_data import MarketDataEngine
from arbitrage.core.state import BotState, ActivePosition
from arbitrage.strategies.base import Opportunity

logger = get_arbitrage_logger("trade_executor")


class TradeExecutor:
    """Executes atomic 2-leg arbitrage trades across exchanges."""

    def __init__(
        self,
        config: ArbitrageConfig,
        exchanges: Dict[str, Any],
    ):
        self.config = config
        self.exchanges = exchanges
        self._contract_sizes: Dict[str, Dict[str, float]] = {}

    def set_contract_sizes(self, sizes: Dict[str, Dict[str, float]]) -> None:
        self._contract_sizes = sizes

    # ─── Entry ────────────────────────────────────────────────────────────

    async def execute_entry(
        self, opp: Opportunity, state: BotState, market_data: MarketDataEngine
    ) -> bool:
        """Open a 2-leg arbitrage position atomically."""
        if self.config.monitoring_only:
            return False

        symbol = opp.symbol
        long_ex = opp.long_exchange
        short_ex = opp.short_exchange
        leverage = self.config.leverage

        first_ex, first_side, second_ex, second_side = self._determine_leg_order(
            long_ex, short_ex
        )

        per_side_usd = self._calculate_position_size(state, long_ex, short_ex)
        if per_side_usd < 4.0:
            return False

        avg_price = (opp.long_price + opp.short_price) / 2
        first_contracts = self._calculate_contracts(first_ex, symbol, per_side_usd, avg_price)
        second_contracts = self._calculate_contracts(second_ex, symbol, per_side_usd, avg_price)
        if first_contracts <= 0 or second_contracts <= 0:
            return False

        await asyncio.gather(
            self._set_leverage(first_ex, symbol, leverage),
            self._set_leverage(second_ex, symbol, leverage),
        )

        try:
            if self.config.dry_run_mode:
                return self._dry_run_entry(
                    opp, state, per_side_usd, first_contracts, second_contracts
                )

            # Step 1: First leg (risky)
            first_result = await self._place_order(
                first_ex, symbol, first_side, first_contracts,
                self._get_order_type(first_ex), offset="open", leverage=leverage
            )
            if not self._check_result(first_ex, first_result):
                logger.warning(
                    f"First leg ({first_ex}) failed for {symbol}: {first_result}"
                )
                return False

            await asyncio.sleep(0.5)
            first_actual, first_fill = await self._verify_position(first_ex, symbol)
            if first_actual == 0:
                logger.warning(f"{first_ex.upper()} no position for {symbol}")
                return False
            first_contracts = self._normalize_size(first_ex, first_actual)

            # Step 2: Second leg
            if second_ex == "okx":
                slippage = 0.0015
                px = round(
                    (opp.long_price * (1 + slippage)) if second_side == "buy"
                    else (opp.short_price * (1 - slippage)),
                    10,
                )
                second_result = await self._place_order(
                    second_ex, symbol, second_side, second_contracts,
                    "limit", price=px, offset="open", leverage=leverage
                )
            else:
                second_result = await self._place_order(
                    second_ex, symbol, second_side, second_contracts,
                    self._get_order_type(second_ex), offset="open", leverage=leverage
                )

            second_ok = self._check_result(second_ex, second_result)
            if not second_ok:
                logger.warning(
                    f"Second leg ({second_ex}) order rejected for {symbol}: {second_result}"
                )
            second_fill = 0.0
            if second_ok:
                await asyncio.sleep(0.5)
                second_actual, second_fill = await self._verify_position(second_ex, symbol)
                if second_actual == 0:
                    second_ok = False
                else:
                    second_contracts = self._normalize_size(second_ex, second_actual)

            if second_ok:
                if first_ex == long_ex:
                    lc, sc = first_contracts, second_contracts
                    lf, sf = first_fill, second_fill
                else:
                    lc, sc = second_contracts, first_contracts
                    lf, sf = second_fill, first_fill

                pos = ActivePosition(
                    strategy=opp.strategy.value,
                    symbol=symbol,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    long_contracts=lc,
                    short_contracts=sc,
                    long_price=lf if lf > 0 else opp.long_price,
                    short_price=sf if sf > 0 else opp.short_price,
                    entry_spread=opp.expected_profit_pct,
                    size_usd=per_side_usd * 2,
                    target_profit=opp.expected_profit_pct,
                )
                state.add_position(pos)
                logger.info(
                    f"ENTRY OK: {symbol} L:{long_ex} S:{short_ex} "
                    f"spread={opp.expected_profit_pct:.3f}%"
                )
                return True
            else:
                # Second leg failed — hedge first leg
                close_side = "sell" if first_side == "buy" else "buy"
                logger.warning(f"Second leg ({second_ex}) failed — hedging {first_ex}")
                hedge_ok = await self._close_with_retries(
                    first_ex, symbol, close_side, first_contracts, leverage
                )
                if not hedge_ok:
                    if first_ex == long_ex:
                        lc, sc = first_contracts, 0
                    else:
                        lc, sc = 0, first_contracts
                    pos = ActivePosition(
                        strategy=opp.strategy.value,
                        symbol=symbol,
                        long_exchange=long_ex,
                        short_exchange=short_ex,
                        long_contracts=lc,
                        short_contracts=sc,
                        long_price=opp.long_price,
                        short_price=opp.short_price,
                        entry_spread=0,
                        size_usd=per_side_usd,
                        exit_threshold=0,
                    )
                    state.add_position(pos)
                    logger.error(f"HEDGE FAILED {first_ex} {symbol}")
                return False

        except Exception as e:
            logger.error(f"Entry error {symbol}: {e}", exc_info=True)
            return False

    # ─── Exit ─────────────────────────────────────────────────────────────

    async def execute_exit(
        self, pos: ActivePosition, state: BotState,
        market_data: MarketDataEngine, reason: str
    ) -> Tuple[bool, float]:
        """Close a 2-leg position. Returns (success, pnl_usd)."""
        if self.config.dry_run_mode:
            return self._dry_run_exit(pos, state, reason)

        symbol = pos.symbol
        leverage = self.config.leverage

        long_closed = pos.long_contracts == 0
        short_closed = pos.short_contracts == 0

        if not long_closed and not short_closed:
            long_closed = await self._close_with_retries(
                pos.long_exchange, symbol, "sell", pos.long_contracts, leverage
            )
            short_closed = await self._close_with_retries(
                pos.short_exchange, symbol, "buy", pos.short_contracts, leverage
            )
        elif not long_closed:
            long_closed = await self._close_with_retries(
                pos.long_exchange, symbol, "sell", pos.long_contracts, leverage
            )
            short_closed = True
        elif not short_closed:
            short_closed = await self._close_with_retries(
                pos.short_exchange, symbol, "buy", pos.short_contracts, leverage
            )
            long_closed = True
        else:
            long_closed = short_closed = True

        if not long_closed or not short_closed:
            if long_closed:
                pos.long_contracts = 0
            if short_closed:
                pos.short_contracts = 0
            return False, 0.0

        pnl = self._estimate_pnl(pos)
        state.remove_position(pos.strategy, pos.symbol)
        logger.info(f"EXIT OK: {symbol} pnl=${pnl:+.4f} reason={reason}")
        return True, pnl

    # ─── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _determine_leg_order(long_ex: str, short_ex: str):
        if long_ex == "okx":
            return short_ex, "sell", long_ex, "buy"
        elif short_ex == "okx":
            return long_ex, "buy", short_ex, "sell"
        else:
            first = min(long_ex, short_ex)
            if first == long_ex:
                return long_ex, "buy", short_ex, "sell"
            return short_ex, "sell", long_ex, "buy"

    def _calculate_position_size(
        self, state: BotState, long_ex: str, short_ex: str
    ) -> float:
        long_bal = state.get_balance(long_ex)
        short_bal = state.get_balance(short_ex)
        pct = self.config.max_position_pct
        
        # Bybit requires minimum $5.4 per side, other exchanges can be smaller
        long_min = 5.4 if long_ex == "bybit" else 0.0
        short_min = 5.4 if short_ex == "bybit" else 0.0
        
        long_side = max(long_min, long_bal * pct)
        short_side = max(short_min, short_bal * pct)
        if long_side > long_bal or short_side > short_bal:
            return 0.0
        return min(long_side, short_side)

    def _calculate_contracts(
        self, exchange: str, symbol: str, usd: float, price: float
    ) -> float:
        """Calculate position size. Returns contract count (OKX/HTX) or qty in base (Bybit)."""
        ct = self._contract_sizes.get(exchange, {}).get(symbol, 1.0)
        if price <= 0 or ct <= 0:
            return 0
        if exchange == "bybit":
            # Bybit: qty is in base currency; ct = qtyStep (min increment)
            raw_qty = usd / price

            # Guardrail: Bybit min order value is 5 USDT (per log retCode=110094).
            # Add a small buffer for rounding down and fees.
            min_notional = 5.0
            buffer = 0.15  # USDT
            if usd < (min_notional + buffer):
                # returning 0 will cause execute_entry() to skip this opportunity
                return 0

            # Round DOWN to qtyStep so we don't exceed risk sizing
            steps = int(raw_qty / ct)
            qty = steps * ct
            if qty <= 0:
                return 0

            # Ensure notional after rounding still meets min_notional
            if qty * price < min_notional:
                # Try rounding UP by one step if it still fits our USD budget (+buffer).
                up_qty = (steps + 1) * ct
                if up_qty * price <= usd + buffer and up_qty * price >= min_notional:
                    return up_qty
                return 0

            return qty
        else:
            # OKX/HTX: integer contract count
            return max(1, int(usd / (price * ct)))

    def _normalize_size(self, exchange: str, raw_size: float) -> float:
        """Normalize verified position size to usable format."""
        if exchange == "bybit":
            return raw_size  # already in base currency
        return int(raw_size)  # OKX/HTX: integer contracts

    @staticmethod
    def _get_order_type(exchange: str) -> str:
        if exchange == "htx":
            return "optimal_5"
        return "market"

    @staticmethod
    def _estimate_pnl(pos: ActivePosition) -> float:
        fee_pct = 0.18
        pnl = pos.size_usd * (pos.entry_spread - fee_pct) / 100
        pnl -= pos.total_fees
        pnl += pos.accumulated_funding
        return pnl

    def _dry_run_entry(self, opp, state, per_side_usd, first_c, second_c) -> bool:
        pos = ActivePosition(
            strategy=opp.strategy.value,
            symbol=opp.symbol,
            long_exchange=opp.long_exchange,
            short_exchange=opp.short_exchange,
            long_contracts=first_c,
            short_contracts=second_c,
            long_price=opp.long_price,
            short_price=opp.short_price,
            entry_spread=opp.expected_profit_pct,
            size_usd=per_side_usd * 2,
        )
        state.add_position(pos)
        logger.info(f"[DRY RUN] ENTRY: {opp.symbol} L:{opp.long_exchange} S:{opp.short_exchange}")
        return True

    def _dry_run_exit(self, pos, state, reason) -> Tuple[bool, float]:
        pnl = self._estimate_pnl(pos)
        state.remove_position(pos.strategy, pos.symbol)
        logger.info(f"[DRY RUN] EXIT: {pos.symbol} pnl=${pnl:+.4f} reason={reason}")
        return True, pnl

    # ─── Exchange Operations ──────────────────────────────────────────────

    async def _place_order(
        self, exchange: str, symbol: str, side: str, contracts: int,
        order_type: str, price: float = 0, offset: str = "open", leverage: int = 1
    ) -> Dict:
        client = self.exchanges[exchange]
        if exchange == "okx":
            tif = "ioc" if order_type == "limit" else ""
            return await client.place_order(symbol, side, contracts, order_type, price, tif)
        elif exchange == "htx":
            return await client.place_order(
                symbol, side, contracts, order_type, offset=offset, lever_rate=leverage
            )
        elif exchange == "bybit":
            # Bybit qty is already in base currency (from _calculate_contracts)
            logger.info(
                f"Bybit order: {symbol} {side} qty={contracts} type={order_type}"
            )
            return await client.place_order(
                symbol, side, contracts, order_type, price=price,
                offset=offset, lever_rate=leverage
            )
        return {"error": f"Unknown exchange: {exchange}"}

    @staticmethod
    def _check_result(exchange: str, result) -> bool:
        if isinstance(result, Exception):
            return False
        if exchange == "okx":
            return result.get("code") == "0" and bool(result.get("data"))
        elif exchange == "htx":
            return result.get("status") == "ok" and bool(result.get("data"))
        elif exchange == "bybit":
            return result.get("retCode") == 0 and bool(result.get("result"))
        return False

    async def _verify_position(
        self, exchange: str, symbol: str
    ) -> Tuple[float, float]:
        """Returns (size, avg_price)."""
        try:
            client = self.exchanges[exchange]
            if exchange == "okx":
                result = await client.get_cross_position(symbol)
                if result.get("code") == "0":
                    for pos in result.get("data", []):
                        sym = (
                            pos.get("instId", "")
                            .replace("-USDT-SWAP", "")
                            .replace("-", "") + "USDT"
                        )
                        if sym == symbol and float(pos.get("pos", 0)) != 0:
                            return abs(float(pos["pos"])), float(pos.get("avgPx", 0) or 0)
            elif exchange == "htx":
                result = await client.get_cross_position(symbol)
                if result.get("status") == "ok":
                    for pos in result.get("data", []):
                        vol = float(pos.get("volume", 0))
                        if vol > 0:
                            return vol, float(pos.get("cost_open", 0) or 0)
            elif exchange == "bybit":
                result = await client.get_cross_position(symbol)
                if result.get("retCode") == 0:
                    for pos in result.get("result", {}).get("list", []):
                        size = float(pos.get("size", 0) or 0)
                        if size > 0:
                            return size, float(pos.get("avgPrice", 0) or 0)
        except Exception as e:
            logger.error(f"Verify position error ({exchange} {symbol}): {e}")
        return 0, 0

    async def _close_with_retries(
        self, exchange: str, symbol: str, side: str, contracts: float, leverage: int
    ) -> bool:
        """Close position with 3 retries and verification."""
        order_type = "opponent" if exchange == "htx" else "market"
        for attempt in range(3):
            try:
                result = await self._place_order(
                    exchange, symbol, side, contracts,
                    order_type, offset="close", leverage=leverage
                )
                if self._check_result(exchange, result):
                    await asyncio.sleep(0.5)
                    remaining, _ = await self._verify_position(exchange, symbol)
                    if remaining == 0:
                        return True
                    if remaining < contracts:
                        contracts = self._normalize_size(exchange, remaining)
                        logger.warning(
                            f"{exchange.upper()} {symbol}: partial, {remaining} left"
                        )
                else:
                    logger.warning(f"{exchange.upper()} close attempt {attempt + 1} failed")
            except Exception as e:
                logger.error(f"{exchange.upper()} close attempt {attempt + 1}: {e}")
            if attempt < 2:
                await asyncio.sleep(1)
        logger.error(f"FAILED to close {exchange.upper()} {symbol} after 3 attempts!")
        return False

    async def _set_leverage(self, exchange: str, symbol: str, leverage: int) -> None:
        try:
            client = self.exchanges[exchange]
            if exchange == "okx":
                await client.set_leverage(symbol, leverage)
            elif exchange == "htx":
                r = await client.set_leverage(symbol, leverage)
                if isinstance(r, dict) and r.get("status") == "error":
                    err = r.get("err-msg", "")
                    if "already" not in err.lower() and "same" not in err.lower():
                        logger.warning(f"HTX leverage: {err}")
            elif exchange == "bybit":
                r = await client.set_leverage(symbol, leverage)
                if isinstance(r, dict) and r.get("retCode") != 0:
                    msg = r.get("retMsg", "")
                    if "not modified" not in msg.lower() and "pm mode" not in msg.lower():
                        logger.warning(f"Bybit leverage: {msg}")
        except Exception:
            pass
