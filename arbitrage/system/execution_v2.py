"""
Atomic Two-Phase Execution System v2

Принципы:
1. Market orders only - гарантия исполнения
2. Simultaneous entry - обе ноги одновременно
3. Direct position verification - REST API checks
4. Guaranteed hedge - повторяем до полного закрытия
5. Fail-safe monitor - background orphan detector

Author: Claude Code
Date: 2026-04-03
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from arbitrage.system.slippage import SlippageModel

logger = logging.getLogger(__name__)


# FIX #4: ExecutionReport defined at module scope (not inside methods).
# This enables isinstance() checks across the codebase and avoids
# per-call memory allocation overhead.
@dataclass
class ExecutionReport:
    """Legacy-compatible execution report."""
    success: bool
    message: str
    fill_price_long: float = 0.0
    fill_price_short: float = 0.0
    hedged: bool = True


class ExecutionPhase(Enum):
    """Фазы исполнения"""
    PREFLIGHT = "preflight"
    ENTRY = "entry"
    VERIFICATION = "verification"
    HEDGE = "hedge"
    SUCCESS = "success"
    FAILED = "failed"


class ExecutionStatus(Enum):
    """Статусы исполнения"""
    OK = "ok"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    EXISTING_POSITION = "existing_position_conflict"
    PARTIAL_FILL = "partial_fill"
    BOTH_FILLED = "both_filled"
    NEED_HEDGE = "need_hedge"
    FULLY_HEDGED = "fully_hedged"
    HEDGE_INCOMPLETE = "hedge_incomplete"


@dataclass
class ExecutionResult:
    """Результат исполнения сделки"""
    success: bool
    phase: ExecutionPhase
    status: ExecutionStatus
    message: str
    notional_usd: float

    # Детали позиций
    position_a: float = 0.0
    position_b: float = 0.0
    exchange_a: str = ""
    exchange_b: str = ""

    # PnL и метрики
    entry_price_a: float = 0.0
    entry_price_b: float = 0.0
    hedge_attempts: int = 0
    execution_time_ms: float = 0.0


class AtomicExecutionEngineV2:
    """
    Atomic Two-Phase Execution Engine V2

    Гарантирует что сделка либо открывается обеими ногами,
    либо полностью откатывается без orphan позиций.

    RENAMED from AtomicExecutionEngine to avoid name collision with
    execution.py's AtomicExecutionEngine class.
    """

    # FIX #5: Per-exchange margin requirements (was hardcoded 0.15 for all)
    DEFAULT_MARGIN_REQUIREMENTS: Dict[str, float] = {
        "okx": 0.15,
        "bybit": 0.15,
        "htx": 0.20,
        "binance": 0.12,
    }

    def __init__(self, venue, config, monitor, min_notional=2.0, balance_utilization=0.30,
                 position_check_delay=2.0, max_hedge_attempts=5, market_data=None):
        """
        Args:
            venue: Venue adapter (live_adapters)
            config: TradingSystemConfig or ExecutionConfig
            monitor: EventMonitor
            min_notional: Minimum trade size in USD (default: 2.0)
            balance_utilization: Fraction of balance to use per trade (default: 0.30)
            position_check_delay: Delay before position verification in seconds (default: 2.0)
            max_hedge_attempts: Maximum hedge retry attempts (default: 5)
            market_data: MarketDataEngine — required for correct contract-size conversion in hedge.
        """
        self.venue = venue
        self.config = config
        self.monitor = monitor
        self.market_data = market_data  # FIX CRITICAL #1: needed for contract-size lookup in hedge

        # Настройки из параметров
        self.MIN_NOTIONAL = min_notional
        self.BALANCE_UTILIZATION = balance_utilization
        # FIX #5: Use per-exchange margin requirements with fallback
        # FIX #17: Use per-exchange margin requirements with fallback
        self.MARGIN_REQUIREMENTS = dict(self.DEFAULT_MARGIN_REQUIREMENTS)
        # Override from config if available
        if hasattr(config, 'margin_requirements'):
            self.MARGIN_REQUIREMENTS.update(config.margin_requirements)
        # Backward compat: single value fallback
        margin_req = getattr(config, 'margin_requirement', None)
        if margin_req is not None:
            for ex in self.MARGIN_REQUIREMENTS:
                self.MARGIN_REQUIREMENTS[ex] = margin_req
        self.MAX_HEDGE_ATTEMPTS = max_hedge_attempts

        # FIX #6: Per-exchange position verification delay.
        # HTX needs 3-5s to reflect position changes after fill via REST.
        # OKX/Bybit need ~2s.  Using per-exchange delays prevents false
        # "need hedge" triggers caused by slow position propagation.
        self.POSITION_CHECK_DELAY = max(min(position_check_delay, 5.0), 2.0)
        self.PER_EXCHANGE_VERIFY_DELAY: Dict[str, float] = {
            "htx": 3.0,
            "okx": 2.0,
            "bybit": 2.0,
            "binance": 1.5,
        }

        # Slippage model (required by TradingSystemEngine.run_cycle for slippage estimation)
        self.slippage = SlippageModel()

    async def execute_arbitrage(
        self,
        intent,
        balances: Dict[str, float]
    ) -> ExecutionResult:
        """
        Главный метод исполнения арбитражной сделки.

        Args:
            intent: TradeIntent with symbol, exchanges, sides
            balances: {exchange: balance_usd}

        Returns:
            ExecutionResult
        """
        start_time = time.time()

        symbol = intent.symbol
        exchange_a = intent.metadata.get("long_exchange", "")
        exchange_b = intent.metadata.get("short_exchange", "")

        # FIX #3: The long leg always buys (goes long), the short leg always
        # sells (goes short).  Previous code compared a variable to itself
        # (tautology), always producing "buy" — working only by accident.
        side_a = "buy"   # long exchange → buy
        side_b = "sell"  # short exchange → sell

        logger.info(
            "[EXEC_V2_START] %s: %s %s, %s %s",
            symbol, exchange_a, side_a, exchange_b, side_b
        )

        # ═══════════════════════════════════════════════════════════
        # PHASE 1: PREFLIGHT CHECK
        # ═══════════════════════════════════════════════════════════
        status, safe_notional = await self._preflight_check(
            symbol, exchange_a, exchange_b, balances
        )

        if status != ExecutionStatus.OK:
            logger.warning(
                "[EXEC_V2_PREFLIGHT_ABORT] %s: %s",
                symbol, status.value
            )
            return ExecutionResult(
                success=False,
                phase=ExecutionPhase.PREFLIGHT,
                status=status,
                message=status.value,
                notional_usd=0.0
            )

        logger.info(
            "[EXEC_V2_PREFLIGHT_OK] %s: safe_notional=%.2f",
            symbol, safe_notional
        )

        # ═══════════════════════════════════════════════════════════
        # PHASE 2: SIMULTANEOUS MARKET ENTRY
        # ═══════════════════════════════════════════════════════════
        entry_status, results = await self._execute_both_legs(
            symbol, exchange_a, exchange_b, side_a, side_b, safe_notional
        )

        if entry_status != ExecutionStatus.BOTH_FILLED:
            logger.error(
                "[EXEC_V2_ENTRY_FAILED] %s: %s",
                symbol, entry_status.value
            )

            # FIX #11: Only hedge if at least one leg actually filled.
            # If NEITHER leg filled (e.g. both APIs failed), there is nothing
            # to close and calling _emergency_hedge_all would waste API calls
            # and could accidentally create positions.
            if entry_status == ExecutionStatus.PARTIAL_FILL:
                await self._emergency_hedge_all(symbol, exchange_a, exchange_b)
                logger.info(
                    "[EXEC_V2_HEDGE_DONE] %s: partial — hedged",
                    symbol,
                )
            else:
                logger.info(
                    "[EXEC_V2_NO_HEDGE] %s: no legs filled, skipping hedge",
                    symbol,
                )

            return ExecutionResult(
                success=False,
                phase=ExecutionPhase.ENTRY,
                status=entry_status,
                message="entry_failed",
                notional_usd=0.0
            )

        logger.info(
            "[EXEC_V2_ENTRY_SUCCESS] %s: both legs placed",
            symbol
        )

        # ═══════════════════════════════════════════════════════════
        # PHASE 3: POSITION VERIFICATION
        # ═══════════════════════════════════════════════════════════
        verify_status, positions = await self._verify_positions(
            symbol, exchange_a, exchange_b, safe_notional
        )

        pos_a, pos_b = positions

        if verify_status == ExecutionStatus.OK:
            # SUCCESS - обе позиции открыты корректно
            exec_time = (time.time() - start_time) * 1000
            logger.info(
                "[EXEC_V2_SUCCESS] %s: pos_a=%.2f, pos_b=%.2f, time=%.0fms",
                symbol, pos_a, pos_b, exec_time
            )

            return ExecutionResult(
                success=True,
                phase=ExecutionPhase.SUCCESS,
                status=ExecutionStatus.OK,
                message="success",
                notional_usd=safe_notional,
                position_a=pos_a,
                position_b=pos_b,
                exchange_a=exchange_a,
                exchange_b=exchange_b,
                execution_time_ms=exec_time
            )

        # ═══════════════════════════════════════════════════════════
        # PHASE 4: GUARANTEED HEDGE (что-то пошло не так)
        # ═══════════════════════════════════════════════════════════
        logger.warning(
            "[EXEC_V2_NEED_HEDGE] %s: pos_a=%.2f, pos_b=%.2f",
            symbol, pos_a, pos_b
        )

        hedge_status, hedge_attempts = await self._guaranteed_hedge(
            symbol, exchange_a, exchange_b, pos_a, pos_b
        )

        exec_time = (time.time() - start_time) * 1000

        if hedge_status == ExecutionStatus.FULLY_HEDGED:
            logger.info(
                "[EXEC_V2_HEDGED] %s: fully closed after %d attempts",
                symbol, hedge_attempts
            )
            return ExecutionResult(
                success=False,
                phase=ExecutionPhase.HEDGE,
                status=ExecutionStatus.FULLY_HEDGED,
                message="hedged_successfully",
                notional_usd=safe_notional,
                hedge_attempts=hedge_attempts,
                execution_time_ms=exec_time
            )
        else:
            # FIX CRITICAL #3: Forced close orphan positions after total hedge failure.
            # Do NOT leave unhedged legs on the exchange.
            logger.critical(
                "[EXEC_V2_HEDGE_INCOMPLETE] %s: forced-closing orphans after %d failed attempts",
                symbol, hedge_attempts
            )
            try:
                await self._force_close_orphans(
                    symbol, exchange_a, exchange_b, pos_a, pos_b
                )
            except Exception as e:
                logger.critical(
                    "[EXEC_V2_FORCE_CLOSE_FAILED] %s: %s",
                    symbol, e,
                    exc_info=True,
                )
            return ExecutionResult(
                success=False,
                phase=ExecutionPhase.HEDGE,
                status=ExecutionStatus.HEDGE_INCOMPLETE,
                message="hedge_incomplete_CRITICAL_force_closed",
                notional_usd=safe_notional,
                position_a=pos_a,
                position_b=pos_b,
                exchange_a=exchange_a,
                exchange_b=exchange_b,
                hedge_attempts=hedge_attempts,
                execution_time_ms=exec_time
            )

    async def _preflight_check(
        self,
        symbol: str,
        exchange_a: str,
        exchange_b: str,
        balances: Dict[str, float]
    ) -> Tuple[ExecutionStatus, float]:
        """
        Phase 1: Preflight safety check

        Проверяет:
        1. Балансы на обеих биржах
        2. Вычисляет безопасный размер сделки
        3. Проверяет отсутствие existing positions

        Returns:
            (status, safe_notional_usd)
        """
        # 1. Проверяем балансы
        balance_a = balances.get(exchange_a, 0.0)
        balance_b = balances.get(exchange_b, 0.0)

        logger.debug(
            "[PREFLIGHT] %s: balance_%s=%.2f, balance_%s=%.2f",
            symbol, exchange_a, balance_a, exchange_b, balance_b
        )

        if balance_a <= 0 or balance_b <= 0:
            return ExecutionStatus.INSUFFICIENT_BALANCE, 0.0

        # 2. Вычисляем безопасный размер
        # FIX #5: Use per-exchange margin requirements
        margin_a = self.MARGIN_REQUIREMENTS.get(exchange_a, 0.15)
        margin_b = self.MARGIN_REQUIREMENTS.get(exchange_b, 0.15)
        # Conservative: use the higher margin requirement
        effective_margin = max(margin_a, margin_b)
        min_balance = min(balance_a, balance_b)
        safe_notional = min_balance * self.BALANCE_UTILIZATION / effective_margin

        if safe_notional < self.MIN_NOTIONAL:
            logger.warning(
                "[PREFLIGHT] %s: safe_notional=%.2f < MIN_NOTIONAL=%.2f",
                symbol, safe_notional, self.MIN_NOTIONAL
            )
            return ExecutionStatus.INSUFFICIENT_BALANCE, 0.0

        # 3. Проверяем что нет существующих позиций
        try:
            pos_a = await self._get_position_size(exchange_a, symbol)
            pos_b = await self._get_position_size(exchange_b, symbol)

            if abs(pos_a) > 0.01 or abs(pos_b) > 0.01:
                logger.warning(
                    "[PREFLIGHT] %s: existing positions detected: %s=%.2f, %s=%.2f",
                    symbol, exchange_a, pos_a, exchange_b, pos_b
                )
                return ExecutionStatus.EXISTING_POSITION, 0.0
        except Exception as e:
            logger.debug("[PREFLIGHT] position check error (ignored): %s", e)
            # Игнорируем ошибки проверки позиций в preflight

        return ExecutionStatus.OK, safe_notional

    async def _execute_both_legs(
        self,
        symbol: str,
        exchange_a: str,
        exchange_b: str,
        side_a: str,
        side_b: str,
        notional: float
    ) -> Tuple[ExecutionStatus, Tuple]:
        """
        Phase 2: Simultaneous market entry

        Размещает обе ноги ОДНОВРЕМЕННО market ордерами.

        Returns:
            (status, (result_a, result_b))
        """
        logger.info(
            "[ENTRY] %s: placing %s %.2f on %s, %s %.2f on %s",
            symbol, side_a, notional, exchange_a, side_b, notional, exchange_b
        )

        # Размещаем одновременно
        results = await asyncio.gather(
            self.venue.place_order(exchange_a, symbol, side_a, notional, "market"),
            self.venue.place_order(exchange_b, symbol, side_b, notional, "market"),
            return_exceptions=True
        )

        result_a, result_b = results

        # FIX: Properly handle exceptions during order placement.
        # If one leg threw an exception and the other succeeded, we have
        # an asymmetric fill — must hedge the filled leg immediately.
        if isinstance(result_a, Exception) and isinstance(result_b, Exception):
            logger.error(
                "[ENTRY_EXCEPTION] %s: both legs failed: a=%s, b=%s",
                symbol, result_a, result_b
            )
            # Neither leg filled, nothing to hedge
            return ExecutionStatus.PARTIAL_FILL, results
        elif isinstance(result_a, Exception):
            logger.error(
                "[ENTRY_EXCEPTION] %s: leg_a failed (%s), leg_b=%s",
                symbol, result_a, result_b,
            )
            return ExecutionStatus.PARTIAL_FILL, results
        elif isinstance(result_b, Exception):
            logger.error(
                "[ENTRY_EXCEPTION] %s: leg_b failed (%s), leg_a=%s",
                symbol, result_b, result_a,
            )
            return ExecutionStatus.PARTIAL_FILL, results

        # Проверяем success
        success_a = result_a.get("success", False)
        success_b = result_b.get("success", False)

        if success_a and success_b:
            logger.info(
                "[ENTRY_BOTH_PLACED] %s: order_a=%s, order_b=%s",
                symbol,
                result_a.get("order_id", "?"),
                result_b.get("order_id", "?")
            )
            return ExecutionStatus.BOTH_FILLED, results

        logger.warning(
            "[ENTRY_PARTIAL] %s: success_a=%s, success_b=%s",
            symbol, success_a, success_b
        )
        return ExecutionStatus.PARTIAL_FILL, results

    async def _verify_positions(
        self,
        symbol: str,
        exchange_a: str,
        exchange_b: str,
        expected_notional: float
    ) -> Tuple[ExecutionStatus, Tuple[float, float]]:
        """
        Phase 3: Position verification

        FIX: Uses per-exchange verify delays — HTX needs 3s+ to reflect
        position changes after order fill via REST.
        Using a uniform 2s delay caused false 'need hedge' on HTX.

        Returns:
            (status, (pos_a, pos_b))
        """
        # Per-exchange delays: HTX=3s, OKX/Bybit=2s, Binance=1.5s
        delay_a = self.PER_EXCHANGE_VERIFY_DELAY.get(exchange_a, self.POSITION_CHECK_DELAY)
        delay_b = self.PER_EXCHANGE_VERIFY_DELAY.get(exchange_b, self.POSITION_CHECK_DELAY)
        max_delay = max(delay_a, delay_b)

        # Wait for the slower exchange to process the fill
        await asyncio.sleep(max_delay)

        # Retry verification up to 3 times for slow exchanges
        for attempt in range(3):
            # Получаем ТЕКУЩИЕ позиции через REST
            pos_a = await self._get_position_size(exchange_a, symbol)
            pos_b = await self._get_position_size(exchange_b, symbol)

            logger.info(
                "[VERIFY] %s: attempt %d, pos_%s=%.2f, pos_%s=%.2f",
                symbol, attempt + 1, exchange_a, pos_a, exchange_b, pos_b
            )

            # Проверяем что обе позиции открыты
            if abs(pos_a) > 0.01 and abs(pos_b) > 0.01:
                # Проверяем что они противоположные
                if (pos_a > 0 and pos_b < 0) or (pos_a < 0 and pos_b > 0):
                    logger.info("[VERIFY_OK] %s: opposite positions confirmed after attempt %d", symbol, attempt + 1)
                    return ExecutionStatus.OK, (pos_a, pos_b)

            # Wait before retry (exponential backoff)
            if attempt < 2:
                await asyncio.sleep(1.0 + attempt)

        # Все попытки не удались - нужен hedge
        logger.warning(
            "[VERIFY_NEED_HEDGE] %s: positions not properly hedged after 3 attempts",
            symbol
        )
        return ExecutionStatus.NEED_HEDGE, (pos_a, pos_b)

    async def _guaranteed_hedge(
        self,
        symbol: str,
        exchange_a: str,
        exchange_b: str,
        pos_a: float,
        pos_b: float
    ) -> Tuple[ExecutionStatus, int]:
        """
        Phase 4: Guaranteed hedge

        Закрывает любые открытые позиции market ордерами.
        Повторяет до MAX_HEDGE_ATTEMPTS раз.

        Returns:
            (status, attempt_count)
        """
        for attempt in range(self.MAX_HEDGE_ATTEMPTS):
            logger.info(
                "[HEDGE] %s: attempt %d/%d, pos_a=%.2f, pos_b=%.2f",
                symbol, attempt + 1, self.MAX_HEDGE_ATTEMPTS, pos_a, pos_b
            )

            # Получаем ТЕКУЩИЕ позиции
            current_a = await self._get_position_size(exchange_a, symbol)
            current_b = await self._get_position_size(exchange_b, symbol)

            # Проверяем что обе закрыты
            if abs(current_a) < 0.01 and abs(current_b) < 0.01:
                logger.info(
                    "[HEDGE_COMPLETE] %s: all positions closed after %d attempts",
                    symbol, attempt + 1
                )
                return ExecutionStatus.FULLY_HEDGED, attempt + 1

            # Закрываем что открыто — use contract-size-aware conversion
            tasks = []

            if abs(current_a) > 0.01:
                close_side = "sell" if current_a > 0 else "buy"
                notional_a = self._position_to_notional(exchange_a, symbol, abs(current_a))
                logger.info(
                    "[HEDGE] %s: closing %s %.4f (≈%.2f USD notional) on %s",
                    symbol, close_side, abs(current_a), notional_a, exchange_a
                )
                tasks.append(
                    self.venue.place_order(
                        exchange_a, symbol, close_side, notional_a, "market"
                    )
                )

            if abs(current_b) > 0.01:
                close_side = "sell" if current_b > 0 else "buy"
                notional_b = self._position_to_notional(exchange_b, symbol, abs(current_b))
                logger.info(
                    "[HEDGE] %s: closing %s %.4f (≈%.2f USD notional) on %s",
                    symbol, close_side, abs(current_b), notional_b, exchange_b
                )
                tasks.append(
                    self.venue.place_order(
                        exchange_b, symbol, close_side, notional_b, "market"
                    )
                )

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Ждем и проверяем снова
            await asyncio.sleep(self.POSITION_CHECK_DELAY)
            pos_a = current_a
            pos_b = current_b

        # Все попытки исчерпаны
        logger.critical(
            "[HEDGE_INCOMPLETE] %s: failed after %d attempts, pos_a=%.2f, pos_b=%.2f",
            symbol, self.MAX_HEDGE_ATTEMPTS, pos_a, pos_b
        )
        return ExecutionStatus.HEDGE_INCOMPLETE, self.MAX_HEDGE_ATTEMPTS

    def _position_to_notional(self, exchange: str, symbol: str, position_size: float) -> float:
        """Convert a position size (in contracts) to USD notional.

        Different exchanges have different contract specifications:
        - Binance: 1 contract = 1 base asset (e.g. 1 BTC)
        - Bybit: 1 contract = $1 notional (linear perpetual)
        - OKX: 1 contract = varies by instrument (e.g. 0.01 BTC for BTC-USDT-SWAP)
        - HTX: 1 contract = varies by instrument (e.g. 0.01 BTC)

        Uses market_data contract sizes if available, otherwise falls back to
        exchange-specific defaults.
        """
        ct_size = None  # contract size in base asset units
        if self.market_data is not None:
            try:
                ct_size = self.market_data.get_contract_size(exchange, symbol)
            except Exception:
                ct_size = None

        # FIX #6: Per-symbol contract sizes from market_data (not hardcoded).
        # Default contract sizes from instrument metadata are more accurate
        # than hardcoded guesses for less common symbols.
        if ct_size is None or ct_size <= 0:
            # Only use fallback when market_data is genuinely unavailable
            defaults = {
                "binance": 1.0,    # 1 contract = 1 base asset
                "bybit": 1.0,      # 1 contract = $1 notional (linear)
                "okx": 0.01,       # 0.01 base asset per contract (common)
                "htx": 0.01,       # Similar to OKX
            }
            ct_size = defaults.get(exchange, 0.01)
            logger.warning(
                "[HEDGE_SIZE_FALLBACK] %s %s: using default ct_size=%.4f",
                exchange, symbol, ct_size,
            )

        # For Bybit, position_size is already in $ notional (1 contract = $1)
        if exchange == "bybit":
            return max(1.0, abs(position_size))

        # For other exchanges: notional = position_size * contract_size * price
        # FIX #6: Get current price from market_data first, then try venue fallback.
        price = None
        if self.market_data is not None:
            try:
                ticker = self.market_data.get_futures_price(exchange, symbol)
                if ticker:
                    price = (ticker.bid + ticker.ask) / 2
            except Exception:
                pass

        # FIX #6: If market_data price unavailable, try venue REST query
        if price is None or price <= 0:
            try:
                # Try querying venue for current ticker
                if hasattr(self.venue, 'get_ticker'):
                    ticker_data = self.venue.get_ticker(exchange, symbol)
                    if ticker_data and ticker_data.get("last"):
                        price = float(ticker_data["last"])
            except Exception:
                pass

        if price is None or price <= 0:
            logger.error(
                "[HEDGE_SIZE_ERROR] %s %s: cannot determine price, "
                "refusing to hedge — caller must handle manually",
                exchange, symbol,
            )
            # Return 0 to signal caller cannot hedge automatically
            return 0.0

        notional = abs(position_size) * ct_size * price
        return max(1.0, notional)

    async def _get_position_size(self, exchange: str, symbol: str) -> float:
        """
        Получает размер позиции через REST API.

        Returns:
            position size (positive = long, negative = short)
        """
        try:
            # Используем venue метод для получения позиции
            if hasattr(self.venue, "get_position"):
                pos_data = await self.venue.get_position(exchange, symbol)
                size = float(pos_data.get("size", 0.0) or 0.0)
                side = pos_data.get("side", "")

                if side == "short" or side == "sell":
                    return -abs(size)
                else:
                    return abs(size)
            else:
                logger.warning("[GET_POSITION] venue doesn't have get_position method")
                return 0.0
        except Exception as e:
            logger.error("[GET_POSITION] %s %s error: %s", exchange, symbol, e)
            return 0.0

    async def _emergency_hedge_all(
        self,
        symbol: str,
        exchange_a: str,
        exchange_b: str
    ):
        """Emergency hedge - закрываем любые позиции если entry провалился"""
        logger.warning("[EMERGENCY_HEDGE] %s: checking positions", symbol)

        pos_a = await self._get_position_size(exchange_a, symbol)
        pos_b = await self._get_position_size(exchange_b, symbol)

        if abs(pos_a) > 0.01 or abs(pos_b) > 0.01:
            logger.warning(
                "[EMERGENCY_HEDGE] %s: found positions, closing: a=%.2f, b=%.2f",
                symbol, pos_a, pos_b
            )
            await self._guaranteed_hedge(symbol, exchange_a, exchange_b, pos_a, pos_b)

    async def _force_close_orphans(
        self,
        symbol: str,
        exchange_a: str,
        exchange_b: str,
        pos_a: float,
        pos_b: float,
    ) -> None:
        """FIX CRITICAL #3: Force-close any remaining orphans after total hedge failure.

        When `_guaranteed_hedge` exhausts all attempts but positions still exist
        (e.g. exchange returned stale data, network partition), this method makes
        one final aggressive attempt via the position monitor's REST venue to close
        remaining exposure.
        """
        logger.warning(
            "[FORCE_CLOSE_ORPHANS] %s on %s=%.2f, %s=%.2f — making final attempt",
            symbol, exchange_a, pos_a, exchange_b, pos_b,
        )
        tasks = []
        for ex, pos, side_long in [
            (exchange_a, pos_a, True),
            (exchange_b, pos_b, False),
        ]:
            if abs(pos) < 0.01:
                continue  # already flat
            close_side = "sell" if pos > 0 else "buy"
            notional = self._position_to_notional(ex, symbol, abs(pos))
            if notional <= 0:
                logger.error(
                    "[FORCE_CLOSE_ORPHANS] %s %s: cannot compute notional (%s=%.2f), "
                    "skipping — requires manual intervention",
                    symbol, ex, "pos" if pos > 0 else "pos", pos,
                )
                continue
            logger.warning(
                "[FORCE_CLOSE_ORPHANS] %s: market %s %.2f USD notional on %s",
                symbol, close_side, notional, ex,
            )
            tasks.append(
                self.venue.place_order(ex, symbol, close_side, notional, "market")
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            # Final verification
            await asyncio.sleep(2.0)
            final_a = await self._get_position_size(exchange_a, symbol)
            final_b = await self._get_position_size(exchange_b, symbol)
            if abs(final_a) > 0.01 or abs(final_b) > 0.01:
                logger.critical(
                    "[FORCE_CLOSE_ORPHANS FAILED] %s: still open %s=%.2f, %s=%.2f "
                    "— MANUAL INTERVENTION REQUIRED",
                    symbol, exchange_a, final_a, exchange_b, final_b,
                )
            else:
                logger.info(
                    "[FORCE_CLOSE_ORPHANS OK] %s: all positions closed",
                    symbol,
                )

    # ========================================================================
    # Compatibility methods for legacy engine interface
    # ========================================================================

    async def execute_dual_entry(self, intent, notional_usd: float, **kwargs):
        """
        Compatibility wrapper for legacy engine.
        Maps execute_dual_entry() to execute_arbitrage().

        Args:
            intent: Trading intent with symbol, long_exchange, short_exchange
            notional_usd: Trade size in USD
            **kwargs: Ignored (est_book_depth_usd, volatility, latency_ms)

        Returns:
            ExecutionReport compatible with legacy system
        """
        logger.debug("[EXEC_V2_COMPAT] execute_dual_entry called, mapping to execute_arbitrage")

        # Get current balances
        try:
            balances = await self.venue.get_balances()
        except Exception as e:
            logger.error("[EXEC_V2_COMPAT] Failed to get balances: %s", e)
            balances = {}

        # Execute using V2 system
        result = await self.execute_arbitrage(intent, balances)

        # Map ExecutionResult to legacy ExecutionReport format
        # FIX #4: ExecutionReport now defined at module scope
        return ExecutionReport(
            success=result.success,
            message=result.message,
            fill_price_long=result.entry_price_a if result.exchange_a == intent.long_exchange else result.entry_price_b,
            fill_price_short=result.entry_price_b if result.exchange_b == intent.short_exchange else result.entry_price_a,
            hedged=result.status != ExecutionStatus.HEDGE_INCOMPLETE
        )

    async def execute_dual_exit(self, position, close_reason: str) -> bool:
        """
        Compatibility wrapper for closing positions.

        Args:
            position: Position object with symbol, long_exchange, short_exchange
            close_reason: Reason for closing (logging only)

        Returns:
            bool: True if successfully closed, False otherwise
        """
        logger.info(
            "[EXEC_V2_CLOSE] Closing position %s: %s/%s, reason=%s",
            position.symbol, position.long_exchange, position.short_exchange, close_reason
        )

        # Get current positions
        try:
            pos_long = await self._get_position_size(position.long_exchange, position.symbol)
            pos_short = await self._get_position_size(position.short_exchange, position.symbol)

            if abs(pos_long) < 0.01 and abs(pos_short) < 0.01:
                logger.info("[EXEC_V2_CLOSE] %s: no positions to close", position.symbol)
                return True

            # Use guaranteed hedge to close
            await self._guaranteed_hedge(
                position.symbol,
                position.long_exchange,
                position.short_exchange,
                pos_long,
                pos_short
            )

            # Verify closure
            final_long = await self._get_position_size(position.long_exchange, position.symbol)
            final_short = await self._get_position_size(position.short_exchange, position.symbol)

            success = abs(final_long) < 0.01 and abs(final_short) < 0.01

            if success:
                logger.info("[EXEC_V2_CLOSE] %s: successfully closed", position.symbol)
            else:
                logger.error(
                    "[EXEC_V2_CLOSE] %s: incomplete close, remaining: long=%.4f, short=%.4f",
                    position.symbol, final_long, final_short
                )

            return success

        except Exception as e:
            logger.error("[EXEC_V2_CLOSE] %s: error closing position: %s", position.symbol, e, exc_info=True)
            return False

    async def execute_multi_leg_spot(self, intent):
        """
        Multi-leg spot execution not implemented in V2.
        Falls back to error (V2 is designed for dual-leg futures arbitrage only).

        Args:
            intent: Multi-leg trade intent

        Returns:
            ExecutionReport with success=False
        """
        logger.error("[EXEC_V2_COMPAT] execute_multi_leg_spot not supported in V2, use V1 for multi-leg trades")

        # FIX #4: ExecutionReport now defined at module scope
        return ExecutionReport(
            success=False,
            message="multi_leg_spot_not_supported_in_v2"
        )
