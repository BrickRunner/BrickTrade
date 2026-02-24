"""
Основная логика арбитражного бота
"""
import asyncio
from typing import Optional, Dict, Any

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig, calculate_spread, validate_orderbook
from arbitrage.core.state import BotState, ArbitrageOpportunity
from arbitrage.core.risk import RiskManager
from arbitrage.core.execution import ExecutionManager
from arbitrage.core.notifications import NotificationManager
from arbitrage.exchanges import OKXRestClient, HTXRestClient

logger = get_arbitrage_logger("arbitrage")


class ArbitrageEngine:
    """Движок арбитража"""

    def __init__(
        self,
        config: ArbitrageConfig,
        state: BotState,
        risk_manager: RiskManager,
        execution_manager: ExecutionManager,
        okx_client: OKXRestClient,
        htx_client: HTXRestClient,
        notification_manager: Optional[NotificationManager] = None
    ):
        self.config = config
        self.state = state
        self.risk = risk_manager
        self.execution = execution_manager
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.notifications = notification_manager or NotificationManager()

        # Timing
        self.last_check_time = 0
        self.check_interval = 1  # Проверка каждую секунду

        # Для отслеживания времени в позиции
        self.position_open_time = None

    async def start(self) -> None:
        """Запустить движок арбитража"""
        logger.info("Starting arbitrage engine")
        self.state.is_running = True

        # Инициализация
        await self._initialize()

        # Отправка уведомления о запуске
        await self.notifications.notify_bot_started(self.config)

        # Основной цикл
        while self.state.is_running:
            try:
                await self._main_loop()
                await asyncio.sleep(0.1)  # Небольшая задержка
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """Остановить движок арбитража"""
        logger.info("Stopping arbitrage engine")
        self.state.is_running = False

        # Закрытие всех позиций при остановке
        if self.state.is_in_position:
            logger.warning("Closing all positions before shutdown")
            await self.execution.emergency_close_all()

        # Отправка уведомления об остановке со статистикой
        stats = self.state.get_stats()
        await self.notifications.notify_bot_stopped(stats)

    async def _initialize(self) -> None:
        """Инициализация бота"""
        logger.info("Initializing arbitrage bot")

        try:
            # Установка leverage на обеих биржах
            await self.okx_client.set_leverage(self.config.symbol, self.config.leverage)
            await self.htx_client.set_leverage(self.config.symbol, self.config.leverage)
            logger.info(f"Leverage set to {self.config.leverage}x")

            # Получение начального баланса
            await self._update_balances()

            logger.info("Initialization complete")

        except Exception as e:
            logger.error(f"Initialization error: {e}", exc_info=True)
            raise

    async def _main_loop(self) -> None:
        """Основной цикл работы"""
        # Проверка необходимости аварийного закрытия
        should_close, reason = self.risk.should_emergency_close()
        if should_close and self.state.is_in_position:
            logger.critical(f"Emergency close triggered: {reason}")
            await self.execution.emergency_close_all()
            return

        # Получение текущих стаканов
        okx_ob, htx_ob = self.state.get_orderbooks()

        # Проверка наличия данных
        if not okx_ob or not htx_ob:
            return

        # Валидация стаканов
        if not validate_orderbook({"bids": okx_ob.bids, "asks": okx_ob.asks}):
            logger.debug("Invalid OKX orderbook")
            return

        if not validate_orderbook({"bids": htx_ob.bids, "asks": htx_ob.asks}):
            logger.debug("Invalid HTX orderbook")
            return

        # Расчет спредов
        # Spread 1: LONG OKX, SHORT HTX
        spread1 = calculate_spread(htx_ob.best_bid, okx_ob.best_ask)

        # Spread 2: LONG HTX, SHORT OKX
        spread2 = calculate_spread(okx_ob.best_bid, htx_ob.best_ask)

        # Логика торговли
        if not self.state.is_in_position:
            # Поиск возможности входа
            await self._check_entry_opportunity(spread1, spread2, okx_ob, htx_ob)
        else:
            # Проверка выхода
            await self._check_exit_opportunity(spread1, spread2)

    async def _check_entry_opportunity(
        self,
        spread1: float,
        spread2: float,
        okx_ob: Any,
        htx_ob: Any
    ) -> None:
        """Проверить возможность входа в позицию"""

        opportunity = None

        # Spread 1: LONG OKX, SHORT HTX
        if self.risk.validate_spread(spread1, is_entry=True):
            opportunity = ArbitrageOpportunity(
                spread=spread1,
                long_exchange="okx",
                short_exchange="htx",
                long_price=okx_ob.best_ask,
                short_price=htx_ob.best_bid,
                size=self.config.position_size
            )

        # Spread 2: LONG HTX, SHORT OKX
        elif self.risk.validate_spread(spread2, is_entry=True):
            opportunity = ArbitrageOpportunity(
                spread=spread2,
                long_exchange="htx",
                short_exchange="okx",
                long_price=htx_ob.best_ask,
                short_price=okx_ob.best_bid,
                size=self.config.position_size
            )

        if opportunity:
            logger.info(
                f"Arbitrage opportunity found: spread={opportunity.spread:.3f}%, "
                f"LONG {opportunity.long_exchange} @ {opportunity.long_price}, "
                f"SHORT {opportunity.short_exchange} @ {opportunity.short_price}"
            )

            # Отправка уведомления о найденной возможности
            await self.notifications.notify_opportunity_found(
                spread=opportunity.spread,
                long_exchange=opportunity.long_exchange,
                short_exchange=opportunity.short_exchange,
                long_price=opportunity.long_price,
                short_price=opportunity.short_price,
                size=opportunity.size
            )

            # Проверка рисков
            can_enter, reason = self.risk.can_enter_position(
                opportunity.size,
                (opportunity.long_price + opportunity.short_price) / 2
            )

            if not can_enter:
                logger.warning(f"Cannot enter position: {reason}")
                return

            # Исполнение входа
            self.state.current_opportunity = opportunity

            success, message = await self.execution.execute_arbitrage_entry(
                long_exchange=opportunity.long_exchange,
                short_exchange=opportunity.short_exchange,
                long_price=opportunity.long_price,
                short_price=opportunity.short_price,
                size=opportunity.size
            )

            if success:
                logger.info(f"Successfully entered position: {message}")

                # Запоминаем время открытия позиции
                self.position_open_time = asyncio.get_event_loop().time()

                # Отправка уведомления об открытии позиции
                await self.notifications.notify_position_opened(
                    long_exchange=opportunity.long_exchange,
                    short_exchange=opportunity.short_exchange,
                    size=opportunity.size,
                    long_price=opportunity.long_price,
                    short_price=opportunity.short_price,
                    spread=opportunity.spread
                )
            else:
                logger.error(f"Failed to enter position: {message}")
                self.state.current_opportunity = None

                # Отправка уведомления о неудачном входе
                await self.notifications.notify_execution_failed(
                    reason=message,
                    long_exchange=opportunity.long_exchange,
                    short_exchange=opportunity.short_exchange
                )

    async def _check_exit_opportunity(self, spread1: float, spread2: float) -> None:
        """Проверить возможность выхода из позиции"""

        if not self.state.current_opportunity:
            logger.warning("No current opportunity stored")
            return

        # Определяем текущий спред для наших позиций
        current_spread = spread1 if self.state.current_opportunity.long_exchange == "okx" else spread2

        # Проверка условия выхода
        if self.risk.validate_spread(current_spread, is_entry=False):
            logger.info(f"Exit condition met: spread={current_spread:.3f}%")

            # Проверка возможности выхода
            can_exit, reason = self.risk.can_exit_position()

            if not can_exit:
                logger.warning(f"Cannot exit position: {reason}")
                return

            # Исполнение выхода
            success, message = await self.execution.execute_arbitrage_exit()

            if success:
                logger.info(f"Successfully exited position: {message}")

                # Расчет длительности позиции
                duration_seconds = None
                if self.position_open_time:
                    duration_seconds = asyncio.get_event_loop().time() - self.position_open_time
                    self.position_open_time = None

                # Расчет PnL
                pnl = self.state.calculate_pnl()

                # Отправка уведомления о закрытии позиции
                await self.notifications.notify_position_closed(
                    pnl=pnl,
                    long_exchange=self.state.current_opportunity.long_exchange,
                    short_exchange=self.state.current_opportunity.short_exchange,
                    size=self.state.current_opportunity.size,
                    duration_seconds=duration_seconds
                )
            else:
                logger.error(f"Failed to exit position: {message}")

    async def _update_balances(self) -> None:
        """Обновить балансы на биржах"""
        try:
            # OKX баланс
            okx_balance_data = await self.okx_client.get_balance()
            if okx_balance_data.get("code") == "0" and okx_balance_data.get("data"):
                details = okx_balance_data["data"][0].get("details", [])
                for detail in details:
                    if detail.get("ccy") == "USDT":
                        balance = float(detail.get("availBal", 0))
                        self.state.update_balance("okx", balance)
                        break

            # HTX баланс
            htx_balance_data = await self.htx_client.get_balance()
            if htx_balance_data.get("status") == "ok" and htx_balance_data.get("data"):
                for item in htx_balance_data["data"]:
                    if item.get("margin_asset") == "USDT":
                        balance = float(item.get("margin_available", 0))
                        self.state.update_balance("htx", balance)
                        break

            logger.info(
                f"Balances updated: OKX={self.state.okx_balance:.2f}, "
                f"HTX={self.state.htx_balance:.2f}, "
                f"Total={self.state.total_balance:.2f}"
            )

        except Exception as e:
            logger.error(f"Error updating balances: {e}", exc_info=True)

    async def periodic_tasks(self) -> None:
        """Периодические задачи"""
        while self.state.is_running:
            try:
                # Обновление балансов каждые 30 секунд
                await self._update_balances()

                # Логирование статуса рисков
                self.risk.log_risk_status()

                # Логирование статистики
                stats = self.state.get_stats()
                logger.info(f"Stats: {stats}")

                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error in periodic tasks: {e}", exc_info=True)
                await asyncio.sleep(30)
