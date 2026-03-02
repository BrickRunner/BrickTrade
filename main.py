import asyncio
import logging
import signal
import sys
from aiogram import Bot, Dispatcher
from aiogram.filters import Command

from config import BOT_TOKEN
from database import init_db
from scheduler import scheduler_loop
from states import DateForm, InlineThresholdForm
from api import close_session

# Импорт обработчиков
from handlers import basic, settings, thresholds, stats_handlers
from handlers import arbitrage_handlers_simple as arbitrage_handlers

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Глобальная задача планировщика для корректного завершения
scheduler_task = None


def register_handlers():
    """Регистрация всех обработчиков"""

    # Базовые команды
    dp.message.register(basic.cmd_start, Command("start"))
    dp.message.register(basic.cmd_exchangerate_date, Command("exchangerate_date"))
    dp.message.register(basic.process_date, DateForm.waiting_for_date)
    dp.message.register(basic.handle_send_now, lambda m: m.text == "📊 Курсы валют сейчас")

    # Настройки
    dp.message.register(settings.handle_settings, lambda m: m.text == "⚙ Настройки")
    dp.message.register(
        settings.msg_set_time,
        lambda m: (
            m.text and
            m.text.count(":") == 1 and
            1 <= len(m.text) <= 8 and  # От "0:0" до "  23:59  "
            all(part.strip().isdigit() for part in m.text.split(":") if part.strip())
        )
    )
    dp.callback_query.register(settings.cb_set_currencies, lambda c: c.data == "set_currencies")
    dp.callback_query.register(settings.cb_toggle_curr, lambda c: c.data.startswith("toggle_curr:"))
    dp.callback_query.register(settings.cb_set_time, lambda c: c.data == "set_time")
    dp.callback_query.register(settings.cb_set_days, lambda c: c.data == "set_days")
    dp.callback_query.register(settings.cb_toggle_day, lambda c: c.data.startswith("toggle_day:"))
    dp.callback_query.register(settings.cb_set_timezone, lambda c: c.data == "set_timezone")
    dp.callback_query.register(settings.cb_set_tz, lambda c: c.data.startswith("set_tz:"))
    dp.callback_query.register(settings.cb_back, lambda c: c.data == "back_settings")

    # Пороговые значения
    dp.message.register(thresholds.handle_thresholds, lambda m: m.text == "📉 Пороговые значения")
    dp.message.register(thresholds.threshold_value_manual, InlineThresholdForm.entering_value)
    dp.message.register(thresholds.threshold_comment_manual, InlineThresholdForm.entering_comment_manual)
    dp.callback_query.register(thresholds.cb_add_threshold, lambda c: c.data == "add_threshold")
    dp.callback_query.register(thresholds.cb_delete_thresholds, lambda c: c.data == "del_thresholds")
    dp.callback_query.register(
        thresholds.cb_delete_specific_threshold,
        lambda c: c.data.startswith("del_thr:")
    )
    dp.callback_query.register(
        thresholds.cb_threshold_currency,
        lambda c: c.data.startswith("th_curr:")
    )
    dp.callback_query.register(thresholds.cb_back_main, lambda c: c.data == "back_main")

    # Статистика
    dp.message.register(stats_handlers.handle_stats, lambda m: m.text == "📈 Статистика")
    dp.callback_query.register(stats_handlers.cb_stats, lambda c: c.data == "stats")
    dp.callback_query.register(stats_handlers.cb_stats_period, lambda c: c.data.startswith("stats_curr:"))
    dp.callback_query.register(stats_handlers.cb_show_graph, lambda c: c.data.startswith("stats_period:"))

    # Арбитраж (multi-strategy)
    dp.message.register(arbitrage_handlers.handle_arbitrage_menu, lambda m: m.text == "⚡ Арбитраж")

    # Действия арбитража
    dp.callback_query.register(arbitrage_handlers.cb_arb_multi_start, lambda c: c.data == "arb_multi_start")
    dp.callback_query.register(arbitrage_handlers.cb_arb_multi_stop, lambda c: c.data == "arb_multi_stop")
    dp.callback_query.register(arbitrage_handlers.cb_arb_scan_now, lambda c: c.data == "arb_scan_now")
    dp.callback_query.register(arbitrage_handlers.cb_arb_stats, lambda c: c.data == "arb_stats")
    dp.callback_query.register(arbitrage_handlers.cb_arb_pair_stats, lambda c: c.data == "arb_pair_stats")
    dp.callback_query.register(arbitrage_handlers.cb_arb_history, lambda c: c.data == "arb_history")
    dp.callback_query.register(arbitrage_handlers.cb_arb_funding, lambda c: c.data == "arb_funding")
    dp.callback_query.register(arbitrage_handlers.cb_arb_basis, lambda c: c.data == "arb_basis")
    dp.callback_query.register(arbitrage_handlers.cb_arb_stat_arb, lambda c: c.data == "arb_stat_arb")
    dp.callback_query.register(arbitrage_handlers.cb_arb_emergency_close, lambda c: c.data == "arb_emergency_close")
    dp.callback_query.register(arbitrage_handlers.cb_arb_emergency_confirm, lambda c: c.data == "arb_emergency_confirm")
    dp.callback_query.register(arbitrage_handlers.cb_arb_settings, lambda c: c.data == "arb_settings")
    dp.callback_query.register(arbitrage_handlers.cb_arb_menu, lambda c: c.data == "arb_menu")


async def shutdown(signal_name: str = None):
    """Корректное завершение работы бота"""
    if signal_name:
        logger.info(f"Received exit signal {signal_name}")
    else:
        logger.info("Shutting down...")

    global scheduler_task

    # Отмена задачи планировщика
    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled")

    # Закрытие арбитражных сессий
    try:
        from handlers import arbitrage_handlers_simple as arb
        if arb._router:
            arb._router.stop()
        if arb._state:
            arb._state.is_running = False
        if arb._router_task and not arb._router_task.done():
            arb._router_task.cancel()
            try:
                await arb._router_task
            except asyncio.CancelledError:
                pass
        for client in arb._exchanges.values():
            if client and hasattr(client, 'close'):
                try:
                    await client.close()
                except Exception:
                    pass
            elif client and hasattr(client, 'session') and client.session:
                try:
                    await client.session.close()
                except Exception:
                    pass
        arb._router_task = None
        arb._router = None
        arb._state = None
        arb._exchanges = {}
        logger.info("Arbitrage sessions closed")
    except Exception as e:
        logger.debug(f"Arbitrage cleanup: {e}")

    # Закрытие HTTP-сессии
    await close_session()
    logger.info("HTTP session closed")

    # Закрытие сессии бота
    await bot.session.close()
    logger.info("Bot session closed")

    logger.info("Shutdown complete")


async def main():
    """Основная функция запуска бота"""
    global scheduler_task

    logger.info("Starting bot...")

    try:
        # Инициализация БД
        await init_db()
        logger.info("Database initialized")

        # Регистрация обработчиков
        register_handlers()
        logger.info("Handlers registered")

        # Запуск планировщика в фоне
        scheduler_task = asyncio.create_task(scheduler_loop(bot))
        logger.info("Scheduler started")

        # Запуск polling
        logger.info("Starting polling...")
        await dp.start_polling(bot)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        await shutdown("KeyboardInterrupt")
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}", exc_info=True)
        await shutdown()
        raise
    finally:
        # Финальная очистка
        try:
            await shutdown()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")


def handle_signal(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logger.info(f"Received signal {signum}")
    # Создаем задачу для корректного завершения
    asyncio.create_task(shutdown(f"Signal {signum}"))
    sys.exit(0)


if __name__ == "__main__":
    # Регистрация обработчиков сигналов для Windows и Unix
    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except AttributeError:
        # Windows не поддерживает SIGTERM
        signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Critical error: {e}", exc_info=True)
        sys.exit(1)