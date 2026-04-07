"""
Position Monitor - Fail-Safe Background Task

Постоянно проверяет все открытые позиции и автоматически
закрывает "orphan" позиции (только на одной бирже без hedge).

Author: Claude Code
Date: 2026-04-03
"""

import asyncio
import logging
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Background task для обнаружения и закрытия незахеджированных позиций.

    Работает независимо от основной торговой логики как последняя линия защиты.
    """

    def __init__(self, venue, exchanges: List[str], check_interval: int = 30):
        """
        Args:
            venue: Venue adapter с методами get_position, place_order
            exchanges: Список бирж для мониторинга ['okx', 'htx', 'bybit']
            check_interval: Интервал проверки в секундах (default 30)
        """
        self.venue = venue
        self.exchanges = exchanges
        self.check_interval = check_interval

        # Статистика
        self.checks_performed = 0
        self.orphans_detected = 0
        self.orphans_closed = 0
        self.last_check_time = None
        self.errors_count = 0

        # Флаг для остановки
        self._stop_flag = False

    async def run_forever(self):
        """
        Основной loop монитора.

        Запускается как background task через asyncio.create_task()
        """
        logger.info("[POSITION_MONITOR] Starting monitor, check_interval=%ds", self.check_interval)

        while not self._stop_flag:
            try:
                await self.check_and_hedge_orphans()
                self.checks_performed += 1
                self.last_check_time = datetime.now()

            except Exception as e:
                self.errors_count += 1
                logger.error("[POSITION_MONITOR] Error in check cycle: %s", e, exc_info=True)

            # Ждем перед следующей проверкой
            await asyncio.sleep(self.check_interval)

        logger.info("[POSITION_MONITOR] Stopped")

    def stop(self):
        """Останавливает monitor"""
        logger.info("[POSITION_MONITOR] Stop requested")
        self._stop_flag = True

    async def check_and_hedge_orphans(self):
        """
        Основная логика проверки:
        1. Получаем все позиции на всех биржах
        2. Группируем по символу
        3. Проверяем что каждая позиция захеджирована
        4. Закрываем orphan позиции
        """
        logger.debug("[POSITION_MONITOR] Starting check cycle")

        # Получаем все позиции
        positions = await self._get_all_positions()

        if not positions:
            logger.debug("[POSITION_MONITOR] No open positions")
            return

        # Группируем по символу
        by_symbol = defaultdict(dict)
        for exchange, symbol, size in positions:
            by_symbol[symbol][exchange] = size

        logger.debug(
            "[POSITION_MONITOR] Found %d symbols with positions: %s",
            len(by_symbol),
            list(by_symbol.keys())
        )

        # Проверяем каждый symbol
        for symbol, exchanges_map in by_symbol.items():
            await self._check_symbol_hedge(symbol, exchanges_map)

    async def _check_symbol_hedge(self, symbol: str, exchanges_map: Dict[str, float]):
        """
        Проверяет что позиции по символу корректно захеджированы.

        Правильный hedge = 2 биржи с ПРОТИВОПОЛОЖНЫМИ позициями.

        Args:
            symbol: Trading pair (e.g. BTCUSDT)
            exchanges_map: {exchange: position_size}
        """
        num_exchanges = len(exchanges_map)

        # CASE 1: Позиция только на ОДНОЙ бирже - ORPHAN!
        if num_exchanges == 1:
            exchange = list(exchanges_map.keys())[0]
            size = exchanges_map[exchange]

            logger.critical(
                "[ORPHAN_DETECTED] %s on %s: size=%.4f (no hedge on other exchange)",
                symbol, exchange, size
            )

            self.orphans_detected += 1
            await self._emergency_close_position(exchange, symbol, size)
            return

        # CASE 2: Позиции на 2+ биржах - проверяем что они противоположные
        if num_exchanges >= 2:
            # Берем первые 2 биржи
            exchanges_list = list(exchanges_map.keys())
            pos_1 = exchanges_map[exchanges_list[0]]
            pos_2 = exchanges_map[exchanges_list[1]]

            # Проверяем что они противоположные
            if (pos_1 > 0 and pos_2 < 0) or (pos_1 < 0 and pos_2 > 0):
                # Хороший hedge
                logger.debug(
                    "[HEDGE_OK] %s: %s=%.4f, %s=%.4f",
                    symbol, exchanges_list[0], pos_1, exchanges_list[1], pos_2
                )

                # Проверяем размеры (должны быть примерно равны)
                ratio = abs(pos_1) / abs(pos_2) if abs(pos_2) > 0 else 999
                if ratio < 0.8 or ratio > 1.2:
                    logger.warning(
                        "[HEDGE_IMBALANCE] %s: ratio=%.2f (should be ~1.0)",
                        symbol, ratio
                    )
                    # TODO: можно добавить автоматическую балансировку

            else:
                # Обе позиции в одну сторону - BAD!
                logger.critical(
                    "[HEDGE_ERROR] %s: BOTH positions same side! %s=%.4f, %s=%.4f",
                    symbol, exchanges_list[0], pos_1, exchanges_list[1], pos_2
                )

                self.orphans_detected += 2
                # Закрываем обе
                await self._emergency_close_position(exchanges_list[0], symbol, pos_1)
                await self._emergency_close_position(exchanges_list[1], symbol, pos_2)

        # CASE 3: Позиции на 3+ биржах - странная ситуация
        if num_exchanges > 2:
            logger.warning(
                "[MULTI_EXCHANGE_POSITION] %s: positions on %d exchanges (unusual)",
                symbol, num_exchanges
            )

    async def _get_all_positions(self) -> List[Tuple[str, str, float]]:
        """
        Получает все открытые позиции на всех биржах.

        Returns:
            List of (exchange, symbol, size)
            size > 0 = long, size < 0 = short
        """
        all_positions = []

        for exchange in self.exchanges:
            try:
                positions = await self._get_exchange_positions(exchange)
                all_positions.extend(positions)
            except Exception as e:
                logger.warning(
                    "[POSITION_MONITOR] Failed to get positions from %s: %s",
                    exchange, e
                )

        return all_positions

    async def _get_exchange_positions(self, exchange: str) -> List[Tuple[str, str, float]]:
        """
        Получает позиции с одной биржи.

        Returns:
            List of (exchange, symbol, size)
        """
        try:
            # Используем venue метод если есть
            if hasattr(self.venue, "get_all_positions"):
                positions_data = await self.venue.get_all_positions(exchange)
            else:
                logger.debug("[POSITION_MONITOR] venue doesn't have get_all_positions, using fallback")
                # Fallback - пробуем через прямой REST клиент
                return await self._get_positions_direct(exchange)

            # Парсим результат
            positions = []
            for pos_data in positions_data:
                symbol = pos_data.get("symbol", "")
                size = float(pos_data.get("size", 0) or 0)
                side = pos_data.get("side", "")

                if abs(size) < 0.001:  # Игнорируем очень маленькие позиции
                    continue

                # Нормализуем size (negative для short)
                if side in ["short", "sell"]:
                    size = -abs(size)
                else:
                    size = abs(size)

                positions.append((exchange, symbol, size))

            return positions

        except Exception as e:
            logger.error("[POSITION_MONITOR] Error getting positions from %s: %s", exchange, e)
            return []

    async def _get_positions_direct(self, exchange: str) -> List[Tuple[str, str, float]]:
        """
        Fallback метод - прямая проверка через REST клиент.

        Returns:
            List of (exchange, symbol, size)
        """
        # TODO: Implement direct REST position check per exchange
        # For now, return empty
        logger.debug("[POSITION_MONITOR] Direct position check not implemented for %s", exchange)
        return []

    async def _emergency_close_position(self, exchange: str, symbol: str, size: float):
        """
        EMERGENCY: Закрывает позицию market ордером.

        Args:
            exchange: Exchange name
            symbol: Trading pair
            size: Position size (positive=long, negative=short)
        """
        if abs(size) < 0.001:
            logger.debug("[EMERGENCY_CLOSE] %s %s: position too small, skipping", exchange, symbol)
            return

        close_side = "sell" if size > 0 else "buy"
        notional = abs(size) * 100  # Approximate notional (will be corrected by exchange)

        logger.warning(
            "[EMERGENCY_CLOSE] %s %s: closing %s %.4f (notional ~%.2f)",
            exchange, symbol, close_side, abs(size), notional
        )

        try:
            result = await self.venue.place_order(
                exchange, symbol, close_side, notional, "market"
            )

            if result.get("success"):
                logger.info(
                    "[EMERGENCY_CLOSE_SUCCESS] %s %s: order_id=%s",
                    exchange, symbol, result.get("order_id", "?")
                )
                self.orphans_closed += 1
            else:
                logger.error(
                    "[EMERGENCY_CLOSE_FAILED] %s %s: %s",
                    exchange, symbol, result.get("message", "unknown")
                )

        except Exception as e:
            logger.error(
                "[EMERGENCY_CLOSE_ERROR] %s %s: %s",
                exchange, symbol, e, exc_info=True
            )

        # Ждем немного перед проверкой
        await asyncio.sleep(2)

        # Проверяем что закрылось
        try:
            if hasattr(self.venue, "get_position"):
                pos_data = await self.venue.get_position(exchange, symbol)
                remaining = float(pos_data.get("size", 0) or 0)

                if abs(remaining) > 0.001:
                    logger.warning(
                        "[EMERGENCY_CLOSE_INCOMPLETE] %s %s: remaining=%.4f",
                        exchange, symbol, remaining
                    )
                else:
                    logger.info(
                        "[EMERGENCY_CLOSE_VERIFIED] %s %s: position fully closed",
                        exchange, symbol
                    )
        except Exception as e:
            logger.debug("[EMERGENCY_CLOSE] verification error: %s", e)

    def get_stats(self) -> Dict[str, any]:
        """Возвращает статистику монитора"""
        return {
            "checks_performed": self.checks_performed,
            "orphans_detected": self.orphans_detected,
            "orphans_closed": self.orphans_closed,
            "errors_count": self.errors_count,
            "last_check_time": self.last_check_time.isoformat() if self.last_check_time else None,
            "check_interval_sec": self.check_interval,
        }
