from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.filters import Command

from api import close_session
from config import BOT_TOKEN
from database import init_db
from handlers import arbitrage_handlers_simple as arbitrage_handlers
from handlers import basic, settings, short_handlers, stats_handlers, stock_handlers, thresholds
from market_intelligence.integration import shutdown_market_intelligence
from healthcheck import start_healthcheck_server, stop_healthcheck_server
from scheduler import scheduler_loop
from states import DateForm, InlineThresholdForm


from arbitrage.utils.logger import HourlyRotatingFileHandler

_bot_file_handler = HourlyRotatingFileHandler("logs", "bot.log", level=logging.INFO)
_bot_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[_bot_file_handler, logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler_task = None


def register_handlers() -> None:
    dp.message.register(basic.cmd_start, Command("start"))
    dp.message.register(basic.cmd_exchangerate_date, Command("exchangerate_date"))
    dp.message.register(basic.process_date, DateForm.waiting_for_date)
    dp.message.register(basic.handle_send_now, lambda m: m.text == "📊 Курсы валют сейчас")

    dp.message.register(settings.handle_settings, lambda m: m.text == "⚙ Настройки")
    dp.message.register(
        settings.msg_set_time,
        lambda m: (
            m.text
            and m.text.count(":") == 1
            and 1 <= len(m.text) <= 8
            and all(part.strip().isdigit() for part in m.text.split(":") if part.strip())
        ),
    )
    dp.callback_query.register(settings.cb_set_currencies, lambda c: c.data == "set_currencies")
    dp.callback_query.register(settings.cb_toggle_curr, lambda c: c.data.startswith("toggle_curr:"))
    dp.callback_query.register(settings.cb_set_time, lambda c: c.data == "set_time")
    dp.callback_query.register(settings.cb_set_days, lambda c: c.data == "set_days")
    dp.callback_query.register(settings.cb_toggle_day, lambda c: c.data.startswith("toggle_day:"))
    dp.callback_query.register(settings.cb_set_timezone, lambda c: c.data == "set_timezone")
    dp.callback_query.register(settings.cb_set_tz, lambda c: c.data.startswith("set_tz:"))
    dp.callback_query.register(settings.cb_back, lambda c: c.data == "back_settings")

    dp.message.register(thresholds.handle_thresholds, lambda m: m.text == "📉 Пороговые значения")
    dp.message.register(thresholds.threshold_value_manual, InlineThresholdForm.entering_value)
    dp.message.register(thresholds.threshold_comment_manual, InlineThresholdForm.entering_comment_manual)
    dp.callback_query.register(thresholds.cb_add_threshold, lambda c: c.data == "add_threshold")
    dp.callback_query.register(thresholds.cb_delete_thresholds, lambda c: c.data == "del_thresholds")
    dp.callback_query.register(thresholds.cb_delete_specific_threshold, lambda c: c.data.startswith("del_thr:"))
    dp.callback_query.register(thresholds.cb_threshold_currency, lambda c: c.data.startswith("th_curr:"))
    dp.callback_query.register(thresholds.cb_back_main, lambda c: c.data == "back_main")

    dp.message.register(stats_handlers.handle_stats, lambda m: m.text == "📈 Статистика")
    dp.callback_query.register(stats_handlers.cb_stats, lambda c: c.data == "stats")
    dp.callback_query.register(stats_handlers.cb_stats_period, lambda c: c.data.startswith("stats_curr:"))
    dp.callback_query.register(stats_handlers.cb_show_graph, lambda c: c.data.startswith("stats_period:"))

    dp.message.register(arbitrage_handlers.handle_arbitrage_menu, lambda m: m.text == "⚡ Арбитраж")
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
    dp.callback_query.register(arbitrage_handlers.cb_arb_reset_kill_switch, lambda c: c.data == "arb_reset_ks")
    dp.callback_query.register(arbitrage_handlers.cb_arb_settings, lambda c: c.data == "arb_settings")
    dp.callback_query.register(arbitrage_handlers.cb_arb_menu, lambda c: c.data == "arb_menu")

    # Short-bot (Overheat Detector)
    dp.message.register(short_handlers.handle_short_menu, lambda m: m.text == "🔻 Шорт-бот")
    dp.callback_query.register(short_handlers.cb_short_menu, lambda c: c.data == "short_menu")
    dp.callback_query.register(short_handlers.cb_short_scan_now, lambda c: c.data == "short_scan_now")
    dp.callback_query.register(short_handlers.cb_short_auto_on, lambda c: c.data == "short_auto_on")
    dp.callback_query.register(short_handlers.cb_short_auto_off, lambda c: c.data == "short_auto_off")
    dp.callback_query.register(short_handlers.cb_short_tf, lambda c: c.data and c.data.startswith("short_tf:"))
    dp.callback_query.register(short_handlers.cb_short_last, lambda c: c.data == "short_last")
    dp.callback_query.register(short_handlers.cb_short_settings, lambda c: c.data == "short_settings")
    dp.callback_query.register(short_handlers.cb_short_exec_on, lambda c: c.data == "short_exec_on")
    dp.callback_query.register(short_handlers.cb_short_exec_off, lambda c: c.data == "short_exec_off")
    dp.callback_query.register(short_handlers.cb_short_exec_symbol, lambda c: c.data and c.data.startswith("short_exec:"))
    dp.callback_query.register(short_handlers.cb_short_positions, lambda c: c.data == "short_positions")
    dp.callback_query.register(short_handlers.cb_short_close, lambda c: c.data and c.data.startswith("short_close:"))
    dp.callback_query.register(short_handlers.cb_short_close_all, lambda c: c.data == "short_close_all")
    dp.callback_query.register(short_handlers.cb_short_clear_history, lambda c: c.data == "short_clear_history")
    # Short-bot settings pickers
    dp.callback_query.register(short_handlers.cb_short_set_size, lambda c: c.data == "short_set_size")
    dp.callback_query.register(short_handlers.cb_short_size_val, lambda c: c.data and c.data.startswith("short_size_val:"))
    dp.callback_query.register(short_handlers.cb_short_set_lev, lambda c: c.data == "short_set_lev")
    dp.callback_query.register(short_handlers.cb_short_lev_val, lambda c: c.data and c.data.startswith("short_lev_val:"))
    dp.callback_query.register(short_handlers.cb_short_set_sl, lambda c: c.data == "short_set_sl")
    dp.callback_query.register(short_handlers.cb_short_sl_val, lambda c: c.data and c.data.startswith("short_sl_val:"))
    dp.callback_query.register(short_handlers.cb_short_set_tp, lambda c: c.data == "short_set_tp")
    dp.callback_query.register(short_handlers.cb_short_tp_val, lambda c: c.data and c.data.startswith("short_tp_val:"))
    dp.callback_query.register(short_handlers.cb_short_set_maxpos, lambda c: c.data == "short_set_maxpos")
    dp.callback_query.register(short_handlers.cb_short_maxpos_val, lambda c: c.data and c.data.startswith("short_maxpos_val:"))
    dp.callback_query.register(short_handlers.cb_short_set_minscore, lambda c: c.data == "short_set_minscore")
    dp.callback_query.register(short_handlers.cb_short_minscore_val, lambda c: c.data and c.data.startswith("short_minscore_val:"))

    # Stock trading handlers (MOEX via BCS)
    dp.message.register(stock_handlers.handle_stocks_menu, lambda m: m.text in ("Stocks", "📈 Акции"))
    dp.callback_query.register(stock_handlers.cb_stock_start, lambda c: c.data == "stock_start")
    dp.callback_query.register(stock_handlers.cb_stock_stop, lambda c: c.data == "stock_stop")
    dp.callback_query.register(stock_handlers.cb_stock_stats, lambda c: c.data == "stock_stats")
    dp.callback_query.register(stock_handlers.cb_stock_positions, lambda c: c.data == "stock_positions")
    dp.callback_query.register(stock_handlers.cb_stock_signals, lambda c: c.data == "stock_signals")
    dp.callback_query.register(stock_handlers.cb_stock_settings, lambda c: c.data == "stock_settings")
    dp.callback_query.register(stock_handlers.cb_stock_mode_switch, lambda c: c.data == "stock_mode_switch")
    dp.callback_query.register(stock_handlers.cb_stock_menu, lambda c: c.data == "stock_menu")
    dp.callback_query.register(stock_handlers.cb_stock_confirm, lambda c: c.data and c.data.startswith("stock_confirm:"))
    dp.callback_query.register(stock_handlers.cb_stock_reject, lambda c: c.data and c.data.startswith("stock_reject:"))
    dp.callback_query.register(stock_handlers.cb_stock_emergency, lambda c: c.data == "stock_emergency")
    dp.callback_query.register(stock_handlers.cb_stock_emergency_confirm, lambda c: c.data == "stock_emergency_confirm")
    # Risk settings
    dp.callback_query.register(stock_handlers.cb_stock_set_exposure, lambda c: c.data == "stock_set_exposure")
    dp.callback_query.register(stock_handlers.cb_stock_set_per_pos, lambda c: c.data == "stock_set_per_pos")
    dp.callback_query.register(stock_handlers.cb_stock_set_max_pos, lambda c: c.data == "stock_set_max_pos")
    dp.callback_query.register(stock_handlers.cb_stock_set_max_trades, lambda c: c.data == "stock_set_max_trades")
    dp.callback_query.register(stock_handlers.cb_stock_exposure_val, lambda c: c.data and c.data.startswith("stock_exposure_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_perpos_val, lambda c: c.data and c.data.startswith("stock_perpos_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_maxpos_val, lambda c: c.data and c.data.startswith("stock_maxpos_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_maxtrades_val, lambda c: c.data and c.data.startswith("stock_maxtrades_val:"))
    # SL / TP / Trailing stop settings
    dp.callback_query.register(stock_handlers.cb_stock_set_sl, lambda c: c.data == "stock_set_sl")
    dp.callback_query.register(stock_handlers.cb_stock_set_tp, lambda c: c.data == "stock_set_tp")
    dp.callback_query.register(stock_handlers.cb_stock_set_trail, lambda c: c.data == "stock_set_trail")
    dp.callback_query.register(stock_handlers.cb_stock_sl_val, lambda c: c.data and c.data.startswith("stock_sl_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_tp_val, lambda c: c.data and c.data.startswith("stock_tp_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_trail_val, lambda c: c.data and c.data.startswith("stock_trail_val:"))
    # Quality filter settings
    dp.callback_query.register(stock_handlers.cb_stock_set_min_conf, lambda c: c.data == "stock_set_min_conf")
    dp.callback_query.register(stock_handlers.cb_stock_set_min_edge, lambda c: c.data == "stock_set_min_edge")
    dp.callback_query.register(stock_handlers.cb_stock_set_cooldown, lambda c: c.data == "stock_set_cooldown")
    dp.callback_query.register(stock_handlers.cb_stock_minconf_val, lambda c: c.data and c.data.startswith("stock_minconf_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_minedge_val, lambda c: c.data and c.data.startswith("stock_minedge_val:"))
    dp.callback_query.register(stock_handlers.cb_stock_cooldown_val, lambda c: c.data and c.data.startswith("stock_cooldown_val:"))


async def shutdown(signal_name: str | None = None) -> None:
    global scheduler_task
    if signal_name:
        logger.info("Received exit signal %s", signal_name)
    else:
        logger.info("Shutting down...")

    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass

    try:
        from handlers import arbitrage_handlers_simple as arb
        await arb.shutdown_arbitrage()
    except Exception:
        pass

    try:
        await stock_handlers.shutdown_stocks()
    except Exception:
        pass

    try:
        await short_handlers.shutdown_short()
    except Exception:
        pass

    await close_session()
    try:
        await shutdown_market_intelligence()
    except Exception:
        pass
    try:
        await stop_healthcheck_server()
    except Exception:
        pass
    await bot.session.close()


async def run_telegram() -> None:
    global scheduler_task
    await init_db()
    register_handlers()
    await start_healthcheck_server()
    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    await dp.start_polling(bot)


async def run_trading() -> None:
    from arbitrage.main import run as run_trading_engine

    await run_trading_engine()


async def main() -> None:
    mode = os.getenv("APP_MODE", "telegram").strip().lower()
    if mode == "trading":
        logger.info("Starting in trading mode")
        await run_trading()
        return

    logger.info("Starting in telegram mode")
    try:
        await run_telegram()
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
