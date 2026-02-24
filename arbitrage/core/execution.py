"""
Модуль исполнения ордеров
"""
import asyncio
from typing import Optional, Dict, Any, Tuple
from datetime import datetime

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.state import BotState, Position
from arbitrage.exchanges import OKXRestClient, HTXRestClient

logger = get_arbitrage_logger("execution")


class ExecutionManager:
    """Менеджер исполнения ордеров"""

    def __init__(
        self,
        config: ArbitrageConfig,
        state: BotState,
        okx_client: OKXRestClient,
        htx_client: HTXRestClient
    ):
        self.config = config
        self.state = state
        self.okx_client = okx_client
        self.htx_client = htx_client

        self.order_timeout = config.order_timeout_ms / 1000  # Convert to seconds

    async def execute_arbitrage_entry(
        self,
        long_exchange: str,
        short_exchange: str,
        long_price: float,
        short_price: float,
        size: float
    ) -> Tuple[bool, str]:
        """
        Исполнить вход в арбитражную позицию

        Returns:
            (success, message)
        """
        logger.info(
            f"Executing arbitrage entry: LONG {long_exchange} @ {long_price}, "
            f"SHORT {short_exchange} @ {short_price}, size={size}"
        )

        # Одновременная отправка ордеров на обе биржи
        try:
            results = await asyncio.gather(
                self._place_order(long_exchange, "buy", long_price, size),
                self._place_order(short_exchange, "sell", short_price, size),
                return_exceptions=True
            )

            long_result, short_result = results

            # Проверка результатов
            long_success = not isinstance(long_result, Exception) and long_result.get("success", False)
            short_success = not isinstance(short_result, Exception) and short_result.get("success", False)

            # Обе ноги исполнены
            if long_success and short_success:
                logger.info("Both legs executed successfully")

                # Сохранение позиций
                await self.state.add_position(Position(
                    exchange=long_exchange,
                    symbol=self.config.symbol,
                    side="LONG",
                    size=size,
                    entry_price=long_price,
                    order_id=long_result.get("order_id")
                ))

                await self.state.add_position(Position(
                    exchange=short_exchange,
                    symbol=self.config.symbol,
                    side="SHORT",
                    size=size,
                    entry_price=short_price,
                    order_id=short_result.get("order_id")
                ))

                return True, "Both positions opened"

            # Только одна нога исполнена - нужен хедж
            elif long_success and not short_success:
                logger.error(f"Only long leg executed, short failed: {short_result}")
                await self._emergency_hedge(long_exchange, "buy", size, long_price)
                return False, "Short leg failed, hedged"

            elif short_success and not long_success:
                logger.error(f"Only short leg executed, long failed: {long_result}")
                await self._emergency_hedge(short_exchange, "sell", size, short_price)
                return False, "Long leg failed, hedged"

            # Обе ноги не исполнены
            else:
                logger.error(f"Both legs failed: long={long_result}, short={short_result}")
                return False, "Both legs failed"

        except Exception as e:
            logger.error(f"Error executing arbitrage entry: {e}", exc_info=True)
            return False, f"Execution error: {str(e)}"

    async def execute_arbitrage_exit(self) -> Tuple[bool, str]:
        """
        Исполнить выход из арбитражной позиции

        Returns:
            (success, message)
        """
        if not self.state.is_in_position:
            return False, "Not in position"

        logger.info("Executing arbitrage exit")

        positions = list(self.state.positions.values())

        if len(positions) != 2:
            logger.error(f"Invalid positions count: {len(positions)}")
            return False, "Invalid positions count"

        # Закрываем обе позиции одновременно
        try:
            tasks = []
            for pos in positions:
                # Для закрытия LONG - sell, для SHORT - buy
                close_side = "sell" if pos.side == "LONG" else "buy"
                tasks.append(self._close_position(pos.exchange, close_side, pos.size))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Проверка результатов
            successes = [
                not isinstance(r, Exception) and r.get("success", False)
                for r in results
            ]

            if all(successes):
                logger.info("Both positions closed successfully")

                # Расчет PnL
                pnl = self._calculate_pnl(positions)
                self.state.record_trade(success=True, pnl=pnl)

                # Очистка позиций
                await self.state.clear_positions()

                return True, f"Positions closed, PnL: {pnl:.2f} USDT"

            else:
                # Частичное закрытие
                logger.error(f"Partial close: {results}")

                # Удаляем только успешно закрытые позиции
                for i, (pos, success) in enumerate(zip(positions, successes)):
                    if success:
                        await self.state.remove_position(pos.exchange, pos.symbol)

                return False, "Partial close, manual intervention needed"

        except Exception as e:
            logger.error(f"Error executing arbitrage exit: {e}", exc_info=True)
            return False, f"Exit error: {str(e)}"

    async def _place_order(
        self,
        exchange: str,
        side: str,
        price: float,
        size: float
    ) -> Dict[str, Any]:
        """Разместить ордер на бирже"""
        # DRY RUN режим - НЕ размещаем реальные ордера
        if self.config.dry_run_mode:
            logger.warning(
                f"[DRY RUN] Would place order: {exchange} {side} {size} @ {price} "
                f"(NO REAL ORDER PLACED)"
            )
            # Возвращаем успешный mock результат
            return {
                "success": True,
                "order_id": f"dry_run_{exchange}_{int(datetime.now().timestamp())}",
                "exchange": exchange,
                "dry_run": True
            }

        try:
            if exchange == "okx":
                result = await asyncio.wait_for(
                    self.okx_client.place_order(
                        symbol=self.config.symbol,
                        side=side,
                        size=size,
                        order_type="limit",
                        price=price,
                        time_in_force="ioc"
                    ),
                    timeout=self.order_timeout
                )

                # Парсим ответ OKX
                if result.get("code") == "0" and result.get("data"):
                    order_data = result["data"][0]
                    return {
                        "success": True,
                        "order_id": order_data.get("ordId"),
                        "exchange": exchange
                    }
                else:
                    return {"success": False, "error": result}

            elif exchange == "htx":
                result = await asyncio.wait_for(
                    self.htx_client.place_order(
                        symbol=self.config.symbol,
                        side=side,
                        size=size,
                        order_type="limit",
                        price=price,
                        time_in_force="ioc"
                    ),
                    timeout=self.order_timeout
                )

                # Парсим ответ HTX
                if result.get("status") == "ok" and result.get("data"):
                    return {
                        "success": True,
                        "order_id": str(result["data"].get("order_id", "")),
                        "exchange": exchange
                    }
                else:
                    return {"success": False, "error": result}

            else:
                return {"success": False, "error": f"Unknown exchange: {exchange}"}

        except asyncio.TimeoutError:
            logger.error(f"Order timeout on {exchange}")
            return {"success": False, "error": "Timeout"}
        except Exception as e:
            logger.error(f"Order error on {exchange}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _close_position(self, exchange: str, side: str, size: float) -> Dict[str, Any]:
        """Закрыть позицию рыночным ордером"""
        # DRY RUN режим - НЕ закрываем реальные позиции
        if self.config.dry_run_mode:
            logger.warning(
                f"[DRY RUN] Would close position: {exchange} {side} {size} "
                f"(NO REAL ORDER PLACED)"
            )
            # Возвращаем успешный mock результат
            return {
                "success": True,
                "exchange": exchange,
                "dry_run": True
            }

        try:
            if exchange == "okx":
                result = await self.okx_client.place_order(
                    symbol=self.config.symbol,
                    side=side,
                    size=size,
                    order_type="market"
                )

                if result.get("code") == "0":
                    return {"success": True, "exchange": exchange}
                else:
                    return {"success": False, "error": result}

            elif exchange == "htx":
                result = await self.htx_client.close_position(
                    symbol=self.config.symbol,
                    side=side,
                    size=size,
                )

                if result.get("status") == "ok":
                    return {"success": True, "exchange": exchange}
                else:
                    return {"success": False, "error": result}

            else:
                return {"success": False, "error": f"Unknown exchange: {exchange}"}

        except Exception as e:
            logger.error(f"Close position error on {exchange}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _emergency_hedge(self, exchange: str, failed_side: str, size: float, price: float) -> None:
        """Аварийный хедж при частичном исполнении"""
        logger.warning(f"Emergency hedge: closing {failed_side} position on {exchange}")

        # Закрываем исполненную позицию рыночным ордером
        close_side = "sell" if failed_side == "buy" else "buy"

        try:
            await self._close_position(exchange, close_side, size)
            logger.info("Emergency hedge completed")
        except Exception as e:
            logger.critical(f"Emergency hedge failed: {e}", exc_info=True)

    def _calculate_pnl(self, positions: list) -> float:
        """Рассчитать PnL от закрытия позиций"""
        if len(positions) != 2:
            return 0.0

        # Получаем текущие цены из стаканов
        okx_ob, htx_ob = self.state.get_orderbooks()

        if not okx_ob or not htx_ob:
            logger.warning("Cannot calculate PnL: orderbooks unavailable")
            return 0.0

        pnl = 0.0

        for pos in positions:
            if pos.exchange == "okx":
                exit_price = okx_ob.best_bid if pos.side == "LONG" else okx_ob.best_ask
            else:  # htx
                exit_price = htx_ob.best_bid if pos.side == "LONG" else htx_ob.best_ask

            if pos.side == "LONG":
                pos_pnl = (exit_price - pos.entry_price) * pos.size
            else:  # SHORT
                pos_pnl = (pos.entry_price - exit_price) * pos.size

            pnl += pos_pnl

        return pnl

    async def emergency_close_all(self) -> None:
        """Аварийно закрыть все позиции"""
        logger.warning("Emergency close all positions")

        if not self.state.positions:
            logger.info("No positions to close")
            return

        tasks = []
        for pos in self.state.positions.values():
            close_side = "sell" if pos.side == "LONG" else "buy"
            tasks.append(self._close_position(pos.exchange, close_side, pos.size))

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.state.clear_positions()
            logger.info("All positions closed")
        except Exception as e:
            logger.critical(f"Emergency close failed: {e}", exc_info=True)
