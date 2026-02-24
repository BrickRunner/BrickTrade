"""
Главный файл для запуска арбитражного бота
"""
import asyncio
import signal
import sys
from typing import Optional

from arbitrage.utils import (
    init_arbitrage_logger,
    get_arbitrage_logger,
    ArbitrageConfig
)
from arbitrage.core import (
    BotState,
    RiskManager,
    ExecutionManager,
    ArbitrageEngine,
    NotificationManager
)
from arbitrage.exchanges import (
    OKXWebSocket,
    OKXRestClient,
    HTXWebSocket,
    HTXRestClient
)

# Mock классы для безопасного тестирования
from arbitrage.test.mock_exchanges import (
    MockOKXWebSocket,
    MockOKXRestClient,
    MockHTXWebSocket,
    MockHTXRestClient
)

# Инициализация логгера с
init_arbitrage_logger()
logger = get_arbitrage_logger("main")


class ArbitrageBot:
    """Главный класс арбитражного бота"""

    def __init__(self, config: ArbitrageConfig):
        self.config = config

        # Компоненты
        self.state = BotState()
        self.okx_rest: Optional[OKXRestClient] = None
        self.htx_rest: Optional[HTXRestClient] = None
        self.okx_ws: Optional[OKXWebSocket] = None
        self.htx_ws: Optional[HTXWebSocket] = None
        self.risk: Optional[RiskManager] = None
        self.execution: Optional[ExecutionManager] = None
        self.engine: Optional[ArbitrageEngine] = None
        self.notification_manager: Optional[NotificationManager] = None

        # Tasks
        self.tasks = []

    def set_notification_manager(self, bot, user_id: int):
        """
        Установить Telegram бота для уведомлений

        Args:
            bot: Экземпляр aiogram Bot
            user_id: ID пользователя для отправки уведомлений
        """
        self.notification_manager = NotificationManager(bot, user_id)
        if self.engine:
            self.engine.notifications = self.notification_manager
        logger.info(f"Notification manager set for user {user_id}")

    async def initialize(self) -> None:
        """Инициализация всех компонентов"""

        # ВАЖНО: Проверка режима работы
        if self.config.mock_mode:
            logger.warning("=" * 60)
            logger.warning("⚠️  MOCK MODE ENABLED - НЕ ИСПОЛЬЗУЮТСЯ РЕАЛЬНЫЕ БИРЖИ ⚠️")
            logger.warning("⚠️  БЕЗОПАСНЫЙ РЕЖИМ - РЕАЛЬНЫЕ ДЕНЬГИ НЕ ТРАТЯТСЯ ⚠️")
            logger.warning("=" * 60)
        else:
            logger.warning("=" * 60)
            logger.warning("🚨 REAL MODE ENABLED - ИСПОЛЬЗУЮТСЯ РЕАЛЬНЫЕ БИРЖИ 🚨")
            logger.warning("🚨 ВНИМАНИЕ: БУДУТ ИСПОЛЬЗОВАТЬСЯ РЕАЛЬНЫЕ ДЕНЬГИ! 🚨")
            logger.warning("=" * 60)

        logger.info("Initializing arbitrage bot")

        try:
            # Валидация конфига
            self.config.validate()

            # Выбор клиентов в зависимости от режима
            if self.config.mock_mode:
                # MOCK режим - безопасно, без реальных денег
                logger.info("🔒 Using MOCK clients (safe mode)")
                self.okx_rest = MockOKXRestClient(self.config.get_okx_config())
                self.htx_rest = MockHTXRestClient(self.config.get_htx_config())
                self.okx_ws = MockOKXWebSocket(self.config.symbol, self.config.okx_testnet)
                self.htx_ws = MockHTXWebSocket(self.config.symbol, self.config.htx_testnet)
            else:
                # REAL режим - используются реальные биржи
                logger.warning("🔓 Using REAL clients (DANGER: real money!)")
                self.okx_rest = OKXRestClient(self.config.get_okx_config())
                self.htx_rest = HTXRestClient(self.config.get_htx_config())
                self.okx_ws = OKXWebSocket(self.config.symbol, self.config.okx_testnet)
                self.htx_ws = HTXWebSocket(self.config.symbol, self.config.htx_testnet)

            # Менеджеры
            self.risk = RiskManager(self.config, self.state)
            self.execution = ExecutionManager(
                self.config,
                self.state,
                self.okx_rest,
                self.htx_rest
            )

            # Движок арбитража
            self.engine = ArbitrageEngine(
                self.config,
                self.state,
                self.risk,
                self.execution,
                self.okx_rest,
                self.htx_rest,
                self.notification_manager
            )

            logger.info("Bot initialized successfully")

        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            raise

    async def start(self) -> None:
        """Запуск бота"""
        logger.info("Starting arbitrage bot")

        try:
            # Запуск WebSocket соединений
            self.tasks.append(
                asyncio.create_task(
                    self.okx_ws.connect(self.state.update_orderbook)
                )
            )
            self.tasks.append(
                asyncio.create_task(
                    self.htx_ws.connect(self.state.update_orderbook)
                )
            )

            # Ждем подключения к обеим биржам
            logger.info("Waiting for WebSocket connections...")
            for _ in range(30):  # 30 секунд таймаут
                if self.state.is_both_connected():
                    logger.info("Both exchanges connected")
                    break
                await asyncio.sleep(1)
            else:
                raise TimeoutError("Failed to connect to exchanges")

            # Запуск движка арбитража
            self.tasks.append(
                asyncio.create_task(self.engine.start())
            )

            # Запуск периодических задач
            self.tasks.append(
                asyncio.create_task(self.engine.periodic_tasks())
            )

            logger.info("Bot started successfully")

            # Ожидание выполнения всех задач
            await asyncio.gather(*self.tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Error starting bot: {e}", exc_info=True)
            raise

    async def stop(self) -> None:
        """Остановка бота"""
        logger.info("Stopping arbitrage bot")

        try:
            # Остановка движка
            if self.engine:
                await self.engine.stop()

            # Отключение WebSocket
            if self.okx_ws:
                await self.okx_ws.disconnect()
            if self.htx_ws:
                await self.htx_ws.disconnect()

            # Отмена всех задач
            for task in self.tasks:
                if not task.done():
                    task.cancel()

            # Ожидание завершения задач
            await asyncio.gather(*self.tasks, return_exceptions=True)

            # Закрытие REST сессий
            if self.okx_rest:
                await self.okx_rest.close()
            if self.htx_rest:
                await self.htx_rest.close()

            logger.info("Bot stopped successfully")

        except Exception as e:
            logger.error(f"Error stopping bot: {e}", exc_info=True)

    async def run(self) -> None:
        """Основной цикл работы"""
        try:
            await self.initialize()
            await self.start()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            await self.stop()


# Глобальный экземпляр бота для обработки сигналов
_bot_instance: Optional[ArbitrageBot] = None


def signal_handler(signum, frame):
    """Обработчик сигналов"""
    logger.info(f"Signal {signum} received, shutting down...")
    if _bot_instance:
        asyncio.create_task(_bot_instance.stop())
    sys.exit(0)


async def main():
    """Главная функция"""
    global _bot_instance

    try:
        # Загрузка конфигурации
        config = ArbitrageConfig.from_env()
        logger.info(f"Configuration loaded: symbol={config.symbol}, leverage={config.leverage}")

        # Создание и запуск бота
        bot = ArbitrageBot(config)
        _bot_instance = bot

        # Регистрация обработчиков сигналов
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Запуск
        await bot.run()

    except Exception as e:
        logger.critical(f"Critical error in main: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        # Использование uvloop для лучшей производительности (опционально)
        try:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            logger.info("Using uvloop for better performance")
        except ImportError:
            logger.info("uvloop not available, using default event loop")

        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)
