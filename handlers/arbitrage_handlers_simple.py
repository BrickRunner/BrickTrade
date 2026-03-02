"""
Telegram handlers for the multi-strategy arbitrage bot.
Auto-start, strategy-based UI, trade notifications.
Supports 3 exchanges: OKX, HTX, Bybit.
"""
import asyncio
from typing import Optional
from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from arbitrage.utils import ArbitrageConfig
from arbitrage.core import BotState, RiskManager, NotificationManager, MarketDataEngine
from arbitrage.strategies import StrategyRouter, TradeExecutor
from arbitrage.exchanges import OKXRestClient, HTXRestClient, BybitRestClient

import logging

logger = logging.getLogger(__name__)

# Global instances
_router: Optional[StrategyRouter] = None
_router_task: Optional[asyncio.Task] = None
_state: Optional[BotState] = None
_exchanges: dict = {}


async def _safe_edit(message, text: str, reply_markup=None, parse_mode="HTML"):
    """Edit message, ignoring 'message is not modified' error."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def _main_keyboard(is_running: bool = True) -> InlineKeyboardMarkup:
    if not is_running:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Старт", callback_data="arb_multi_start")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Возможности", callback_data="arb_scan_now"),
         InlineKeyboardButton(text="📈 Статистика", callback_data="arb_stats")],
        [InlineKeyboardButton(text="📋 История", callback_data="arb_history"),
         InlineKeyboardButton(text="💸 Funding", callback_data="arb_funding")],
        [InlineKeyboardButton(text="📉 Basis", callback_data="arb_basis"),
         InlineKeyboardButton(text="📊 Stat Arb", callback_data="arb_stat_arb")],
        [InlineKeyboardButton(text="🚨 Закрыть всё", callback_data="arb_emergency_close")],
        [InlineKeyboardButton(text="⏹ Стоп", callback_data="arb_multi_stop")],
    ])


def _get_exchange_names() -> str:
    if _exchanges:
        return "/".join(e.upper() for e in _exchanges.keys())
    return "OKX/HTX/Bybit"


async def _ensure_started(bot, user_id: int) -> Optional[str]:
    """Start bot if not running. Returns status text or None."""
    global _router, _router_task, _state, _exchanges

    if _router_task and not _router_task.done():
        return None

    config = ArbitrageConfig.from_env()

    okx_client = OKXRestClient(config.get_okx_config())
    htx_client = HTXRestClient(config.get_htx_config())
    bybit_client = BybitRestClient(config.get_bybit_config())

    _exchanges = {"okx": okx_client, "htx": htx_client, "bybit": bybit_client}
    _state = BotState()
    _state.is_running = True

    market_data = MarketDataEngine(_exchanges)
    risk_manager = RiskManager(config, _state)
    executor = TradeExecutor(config, _exchanges)
    notification_manager = NotificationManager(bot, user_id)

    _router = StrategyRouter(
        config=config,
        state=_state,
        market_data=market_data,
        risk_manager=risk_manager,
        executor=executor,
        notification_manager=notification_manager,
    )

    pairs_count = await _router.initialize()
    # Pass contract sizes to executor
    executor.set_contract_sizes(market_data.contract_sizes)

    _router_task = asyncio.create_task(_router.start())

    status = _router.get_status()
    mode = status["mode"].upper()
    strategies = ", ".join(s.replace("_", " ").title() for s in status["strategies"])

    mode_detail = ""
    if mode == "REAL":
        mode_detail = (
            f"Плечо: {config.leverage}x\n"
            f"Позиция: {config.max_position_pct*100:.0f}% депозита\n"
            f"Стратегии: {strategies}"
        )
    elif mode == "DRY_RUN":
        mode_detail = f"Данные реальные, ордера НЕ размещаются\nСтратегии: {strategies}"
    else:
        mode_detail = f"Только мониторинг\nСтратегии: {strategies}"

    exchange_names = _get_exchange_names()

    await notification_manager.send(
        f"✅ <b>Бот запущен!</b>\n\n"
        f"Биржи: <b>{exchange_names}</b>\n"
        f"Режим: <b>{mode}</b>\n"
        f"Пар: {pairs_count}\n"
        f"{mode_detail}\n\n"
        f"🔔 Уведомления о сделках"
    )

    return (
        f"Запущен ({mode})\n"
        f"Биржи: {exchange_names}\n"
        f"Пар: {pairs_count}\n"
        f"{mode_detail}"
    )


# ─── Menu ──────────────────────────────────────────────────────────────────

async def handle_arbitrage_menu(m: types.Message):
    """Main arbitrage menu — auto-start."""
    try:
        start_msg = await _ensure_started(m.bot, m.from_user.id)
        is_running = _router_task is not None and not _router_task.done()

        if start_msg:
            status_line = f"🟢 {start_msg}"
        elif is_running and _router:
            status_line = _build_status_line()
        else:
            status_line = "⚪ Остановлен"

        exchange_names = _get_exchange_names()
        text = f"⚡ <b>Арбитраж {exchange_names}</b>\n\n{status_line}"
        await m.answer(text, reply_markup=_main_keyboard(is_running), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in arbitrage menu: {e}", exc_info=True)
        await m.answer(f"❌ Ошибка: {str(e)}")


def _build_status_line() -> str:
    """Build status line from current router state."""
    if not _router or not _state:
        return "⚪ Нет данных"

    status = _router.get_status()
    positions = _state.position_count()
    trades = status["total_trades"]
    pnl = status["total_pnl"]
    balance = status["total_balance"]
    mode = status["mode"].upper()

    pos_text = ""
    if positions > 0:
        pos_list = _state.get_all_positions()
        for p in pos_list[:2]:
            dur = p.duration()
            pos_text += f"\n📍 {p.symbol} L:{p.long_exchange.upper()} S:{p.short_exchange.upper()} ({dur:.0f}s)"

    return (
        f"🟢 {mode} | {len(_router.market_data.common_pairs)} пар\n"
        f"💰 Баланс: ${balance:.2f}\n"
        f"📈 Сделок: {trades} | PnL: ${pnl:.4f}\n"
        f"📊 Позиций: {positions}{pos_text}"
    )


# ─── Start / Stop ──────────────────────────────────────────────────────────

async def cb_arb_multi_start(call: types.CallbackQuery):
    try:
        if _router_task and not _router_task.done():
            await call.answer("Уже запущен")
            return

        await call.answer()
        await call.message.edit_text("⏳ <b>Запуск...</b>", parse_mode="HTML")

        start_msg = await _ensure_started(call.bot, call.from_user.id)

        await call.message.edit_text(
            f"🟢 <b>{start_msg}</b>",
            parse_mode="HTML",
            reply_markup=_main_keyboard(True)
        )
    except Exception as e:
        logger.error(f"Error starting: {e}", exc_info=True)
        try:
            await call.message.edit_text(f"❌ {str(e)}", parse_mode="HTML")
        except Exception:
            pass


async def cb_arb_multi_stop(call: types.CallbackQuery):
    global _router_task, _state, _router, _exchanges

    try:
        if not _router_task or _router_task.done():
            await call.answer("Не запущен")
            return

        stats_text = ""
        if _state:
            stats = _state.get_stats()
            if stats.get("total_trades", 0) > 0:
                stats_text = (
                    f"\n\n📊 <b>Итого:</b>\n"
                    f"Сделок: {stats['total_trades']}\n"
                    f"PnL: ${stats['total_pnl']:.4f}"
                )

        if _router:
            _router.stop()
        if _state:
            _state.is_running = False

        if _router_task:
            _router_task.cancel()
            try:
                await _router_task
            except asyncio.CancelledError:
                pass

        # Close exchange sessions
        for client in _exchanges.values():
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

        _router_task = None
        _router = None
        _state = None
        _exchanges = {}

        await call.message.edit_text(
            f"⏹ <b>Остановлен</b>{stats_text}",
            parse_mode="HTML",
            reply_markup=_main_keyboard(False)
        )
        await call.answer("Остановлен")

    except Exception as e:
        logger.error(f"Error stopping: {e}", exc_info=True)
        await call.answer(f"Ошибка: {str(e)}")


# ─── Scan (Opportunities) ──────────────────────────────────────────────────

async def cb_arb_scan_now(call: types.CallbackQuery):
    """Show current opportunities from all strategies."""
    try:
        await call.answer("Сканирование...")

        if not _router:
            await _safe_edit(call.message, "📊 Бот не запущен",
                             reply_markup=_main_keyboard(False))
            return

        results = await _router.scan_all()

        # Positions info
        pos_text = ""
        if _state and _state.position_count() > 0:
            pos_text = "<b>📍 Открытые позиции:</b>\n"
            for p in _state.get_all_positions():
                dur = p.duration()
                pos_text += (
                    f"  {p.strategy}: {p.symbol} "
                    f"L:{p.long_exchange.upper()} S:{p.short_exchange.upper()} "
                    f"({dur:.0f}s)\n"
                )
            pos_text += "\n"

        text = f"{pos_text}📊 <b>Топ возможности</b>:\n\n"
        has_data = False

        for strategy_name, items in results.items():
            if not items:
                continue
            has_data = True
            display_name = strategy_name.replace("_", " ").title()
            text += f"<b>{display_name}:</b>\n"

            for item in items[:5]:
                sym = item.get("symbol", "?")
                if strategy_name == "funding_arb":
                    spread = item.get("funding_spread", 0)
                    ann = item.get("annualized", 0)
                    text += (
                        f"  {sym}: {spread:.4f}% "
                        f"(~{ann:.0f}%/yr) "
                        f"L:{item.get('long_exchange', '?').upper()} "
                        f"S:{item.get('short_exchange', '?').upper()}\n"
                    )
                elif strategy_name == "basis_arb":
                    basis = item.get("basis_pct", 0)
                    text += (
                        f"  {sym}: {basis:+.3f}% "
                        f"({item.get('exchange', '?').upper()} "
                        f"{'contango' if basis > 0 else 'backwd'})\n"
                    )
                elif strategy_name == "stat_arb":
                    z = item.get("z_score", 0)
                    spread = item.get("current_spread", 0)
                    text += (
                        f"  {sym}: z={z:+.2f} spread={spread:.3f}% "
                        f"{item.get('ex1', '?').upper()}↔{item.get('ex2', '?').upper()}\n"
                    )
            text += "\n"

        if not has_data:
            text += "Нет данных (ожидание первых циклов)\n"

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_scan_now")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Stats ──────────────────────────────────────────────────────────────────

async def cb_arb_stats(call: types.CallbackQuery):
    try:
        await call.answer("Статистика...")

        if not _state or not _router:
            await _safe_edit(call.message, "📈 Бот не запущен",
                             reply_markup=_main_keyboard(False))
            return

        status = _router.get_status()
        stats = _state.get_stats()
        metrics = status.get("metrics", {})

        total = stats["total_trades"]
        wins = stats["successful_trades"]
        losses = stats["failed_trades"]
        pnl = stats["total_pnl"]
        balance = stats["total_balance"]
        win_rate = stats["success_rate"]

        # Positions
        pos_text = "Нет активных позиций"
        if _state.position_count() > 0:
            pos_lines = []
            for p in _state.get_all_positions():
                dur = p.duration()
                pos_lines.append(
                    f"  {p.strategy}: {p.symbol} "
                    f"L:{p.long_exchange.upper()} S:{p.short_exchange.upper()} "
                    f"${p.size_usd:.2f} ({dur:.0f}s)"
                )
            pos_text = "\n".join(pos_lines)

        # Balance per exchange
        balance_lines = []
        for ex, bal in stats.get("balances", {}).items():
            balance_lines.append(f"   {ex.upper()}: ${bal:.2f}")

        # Per-strategy stats
        strat_text = ""
        for name, ss in stats.get("strategy_stats", {}).items():
            display = name.replace("_", " ").title()
            sw = ss.get("wins", 0)
            sl = ss.get("losses", 0)
            sp = ss.get("pnl", 0)
            strat_text += f"  {display}: {ss['trades']} (✅{sw} ❌{sl}) ${sp:+.4f}\n"

        # Metrics
        sharpe = metrics.get("sharpe", 0)
        max_dd = metrics.get("max_drawdown", 0)
        avg_cycle = metrics.get("avg_cycle_ms", 0)

        text = (
            f"📈 <b>Статистика</b>\n\n"
            f"💰 Баланс: <b>${balance:.2f}</b>\n"
            f"{chr(10).join(balance_lines)}\n\n"
            f"📊 Сделок: {total} (✅{wins} ❌{losses})\n"
            f"🎯 Win rate: {win_rate:.1f}%\n"
            f"💵 PnL: <b>${pnl:+.4f}</b>\n"
            f"📉 Max DD: ${max_dd:.4f}\n"
            f"📐 Sharpe: {sharpe:.2f}\n"
            f"⏱ Цикл: {avg_cycle:.0f}ms\n\n"
        )

        if strat_text:
            text += f"<b>По стратегиям:</b>\n{strat_text}\n"

        text += f"📍 {pos_text}"

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_stats")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Stats error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── History ────────────────────────────────────────────────────────────────

async def cb_arb_history(call: types.CallbackQuery):
    try:
        await call.answer("Загрузка...")

        from arbitrage.core.trade_history import get_recent_trades, get_overall_stats
        trades = await get_recent_trades(10)
        overall = await get_overall_stats()

        text = "📋 <b>История сделок</b>\n\n"

        if overall.get("total_trades", 0) > 0:
            wr = (overall["wins"] / overall["total_trades"] * 100) if overall["total_trades"] > 0 else 0
            text += (
                f"<b>Всего:</b> {overall['total_trades']} сделок\n"
                f"WR: {wr:.0f}% | PnL: ${overall['total_pnl']:.4f}\n"
                f"Fees: ${overall['total_fees']:.4f} | Funding: ${overall['total_funding']:.4f}\n"
                f"Best: ${overall['best_trade']:.4f} | Worst: ${overall['max_drawdown_trade']:.4f}\n\n"
            )

        if trades:
            text += "<b>Последние:</b>\n"
            for t in trades:
                emoji = "✅" if t.get("pnl_usd", 0) > 0 else "❌"
                duration = t.get("duration_seconds", 0)
                from datetime import datetime
                exit_dt = datetime.fromtimestamp(t["exit_time"]).strftime("%m/%d %H:%M") if t.get("exit_time") else "?"
                text += (
                    f"{emoji} {t['symbol']} ${t['pnl_usd']:.4f} "
                    f"({t['entry_spread']:.2f}%→{t.get('exit_spread', 0):.2f}%) "
                    f"{duration:.0f}s {exit_dt}\n"
                )
        else:
            text += "Нет закрытых сделок"

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_history")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"History error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Per-pair stats ─────────────────────────────────────────────────────────

async def cb_arb_pair_stats(call: types.CallbackQuery):
    try:
        await call.answer("Загрузка...")

        from arbitrage.core.trade_history import get_pair_stats
        pair_stats = await get_pair_stats()

        if not pair_stats:
            text = "📊 <b>Per-pair статистика</b>\n\nНет данных о сделках"
        else:
            text = "📊 <b>Per-pair статистика</b>\n\n"
            for ps in pair_stats[:15]:
                wr = (ps["wins"] / ps["total_trades"] * 100) if ps["total_trades"] > 0 else 0
                emoji = "🟢" if ps["total_pnl"] > 0 else "🔴"
                text += (
                    f"{emoji} <b>{ps['symbol']}</b>\n"
                    f"   Сделок: {ps['total_trades']} (WR: {wr:.0f}%)\n"
                    f"   PnL: ${ps['total_pnl']:.4f} (avg ${ps['avg_pnl']:.4f})\n"
                    f"   Fees: ${ps['total_fees']:.4f} | Funding: ${ps['total_funding']:.4f}\n\n"
                )

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_pair_stats")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Pair stats error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Funding Rates ──────────────────────────────────────────────────────────

async def cb_arb_funding(call: types.CallbackQuery):
    """Show funding rate spreads across exchanges."""
    try:
        await call.answer("Загрузка...")

        if not _router:
            await _safe_edit(call.message, "💸 Бот не запущен",
                             reply_markup=_main_keyboard(False))
            return

        strategy = _router._strategies.get("funding_arb")
        if not strategy:
            await _safe_edit(call.message, "💸 Funding стратегия не включена")
            return

        items = strategy.get_all_spreads(_router.market_data)

        if not items:
            text = "💸 <b>Funding Rates</b>\n\nНет данных"
        else:
            text = "💸 <b>Funding Rate Spreads</b>\n\n"
            for item in items[:15]:
                sym = item["symbol"]
                spread = item["funding_spread"]
                ann = item["annualized"]
                long_ex = item["long_exchange"]
                short_ex = item["short_exchange"]

                rates_text = " | ".join(
                    f"{ex.upper()}:{r:.4f}%"
                    for ex, r in item["rates"].items()
                )

                text += (
                    f"{'🟢' if spread > 0.03 else '⚪'} <b>{sym}</b>\n"
                    f"   Spread: {spread:.4f}% (~{ann:.0f}%/yr)\n"
                    f"   L:{long_ex.upper()} S:{short_ex.upper()}\n"
                    f"   {rates_text}\n\n"
                )

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_funding")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Funding error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Basis (Spot vs Futures) ───────────────────────────────────────────────

async def cb_arb_basis(call: types.CallbackQuery):
    """Show spot-futures basis spreads."""
    try:
        await call.answer("Загрузка...")

        if not _router:
            await _safe_edit(call.message, "📉 Бот не запущен",
                             reply_markup=_main_keyboard(False))
            return

        strategy = _router._strategies.get("basis_arb")
        if not strategy:
            await _safe_edit(call.message, "📉 Basis стратегия не включена")
            return

        items = strategy.get_all_spreads(_router.market_data)

        if not items:
            text = "📉 <b>Spot-Futures Basis</b>\n\nНет данных (нужны spot тикеры)"
        else:
            text = "📉 <b>Spot-Futures Basis</b>\n\n"
            for item in items[:15]:
                basis = item["basis_pct"]
                emoji = "📈" if basis > 0 else "📉"
                state = "contango" if basis > 0 else "backwardation"
                text += (
                    f"{emoji} <b>{item['symbol']}</b> ({item['exchange'].upper()})\n"
                    f"   Basis: {basis:+.3f}% ({state})\n"
                    f"   Spot: ${item['spot_price']:.4f} | Fut: ${item['futures_price']:.4f}\n\n"
                )

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_basis")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Basis error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Statistical Arbitrage ──────────────────────────────────────────────────

async def cb_arb_stat_arb(call: types.CallbackQuery):
    """Show statistical arbitrage z-scores."""
    try:
        await call.answer("Загрузка...")

        if not _router:
            await _safe_edit(call.message, "📊 Бот не запущен",
                             reply_markup=_main_keyboard(False))
            return

        strategy = _router._strategies.get("stat_arb")
        if not strategy:
            await _safe_edit(call.message, "📊 Stat Arb стратегия не включена")
            return

        items = strategy.get_all_spreads(_router.market_data)

        if not items:
            text = "📊 <b>Statistical Arbitrage</b>\n\nНет данных (нужно >= 30 samples)"
        else:
            text = "📊 <b>Stat Arb Z-Scores</b>\n\n"
            for item in items[:15]:
                z = item["z_score"]
                sym = item["symbol"]
                ex1 = item["ex1"].upper()
                ex2 = item["ex2"].upper()
                samples = item["samples"]

                if abs(z) > 2.5:
                    emoji = "🔴"
                elif abs(z) > 1.5:
                    emoji = "🟡"
                else:
                    emoji = "🟢"

                text += (
                    f"{emoji} <b>{sym}</b> {ex1}↔{ex2}\n"
                    f"   z={z:+.2f} | spread={item['current_spread']:.3f}%\n"
                    f"   mean={item['mean']:.3f}% std={item['std']:.3f}% ({samples} samples)\n\n"
                )

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_stat_arb")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Stat arb error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Emergency Close ────────────────────────────────────────────────────────

async def cb_arb_emergency_close(call: types.CallbackQuery):
    try:
        await call.answer("⚠️ Подтвердите!")
        exchange_names = _get_exchange_names()
        await call.message.edit_text(
            f"🚨 <b>ЭКСТРЕННОЕ ЗАКРЫТИЕ</b>\n\n"
            f"Будут закрыты ВСЕ позиции на {exchange_names} по рыночной цене.\n\n"
            f"⚠️ Это может привести к убыткам!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚨 ПОДТВЕРДИТЬ ЗАКРЫТИЕ", callback_data="arb_emergency_confirm")],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Emergency close dialog error: {e}", exc_info=True)


async def cb_arb_emergency_confirm(call: types.CallbackQuery):
    try:
        await call.answer("Закрываем...")

        if not _router:
            await call.message.edit_text(
                "Бот не запущен — нечего закрывать",
                parse_mode="HTML",
                reply_markup=_main_keyboard(False)
            )
            return

        await call.message.edit_text("⏳ <b>Закрываем все позиции...</b>", parse_mode="HTML")

        positions = _state.get_all_positions() if _state else []
        closed = 0
        failed = 0

        for pos in positions:
            try:
                success, pnl = await _router.executor.execute_exit(
                    pos, _state, _router.market_data, "emergency_manual"
                )
                if success:
                    closed += 1
                    if _state:
                        _state.record_trade(pos.strategy, success=(pnl > 0), pnl=pnl)
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"Emergency close {pos.symbol}: {e}")
                failed += 1

        result = f"Закрыто: {closed}\nНе удалось: {failed}\nВсего было: {len(positions)}"

        await call.message.edit_text(
            f"🚨 <b>Emergency Close</b>\n\n<pre>{result}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Emergency confirm error: {e}", exc_info=True)
        await call.message.edit_text(f"❌ {str(e)}", parse_mode="HTML")


# ─── Settings ───────────────────────────────────────────────────────────────

async def cb_arb_settings(call: types.CallbackQuery):
    try:
        await call.answer()

        if not _router:
            await _safe_edit(call.message, "⚙️ Бот не запущен",
                             reply_markup=_main_keyboard(False))
            return

        config = _router.config
        status = _router.get_status()
        strategies = ", ".join(s.replace("_", " ").title() for s in status["strategies"])

        text = (
            "⚙️ <b>Настройки</b>\n\n"
            f"Режим: <b>{status['mode'].upper()}</b>\n"
            f"Стратегии: {strategies}\n\n"
            f"📊 Entry/exit (Funding):\n"
            f"   BTC: {config.funding_btc_threshold}%\n"
            f"   ETH: {config.funding_eth_threshold}%\n"
            f"   ALT: {config.funding_alt_threshold}%\n\n"
            f"📉 Basis close: {config.basis_close_threshold}%\n\n"
            f"📊 Stat Arb:\n"
            f"   Z-entry: {config.stat_arb_z_entry}\n"
            f"   Z-exit: {config.stat_arb_z_exit}\n"
            f"   Window: {config.stat_arb_window}\n\n"
            f"⚡ Leverage: {config.leverage}x\n"
            f"💰 Position: {config.max_position_pct*100:.0f}%\n"
            f"📊 Max concurrent: {config.max_concurrent_positions}\n"
        )

        await _safe_edit(call.message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ])
        )
    except Exception as e:
        logger.error(f"Settings error: {e}", exc_info=True)
        await _safe_edit(call.message, f"❌ {str(e)}")


# ─── Back to Menu ──────────────────────────────────────────────────────────

async def cb_arb_menu(call: types.CallbackQuery):
    try:
        await call.answer()
        is_running = _router_task is not None and not _router_task.done()

        if is_running and _router:
            status_line = _build_status_line()
        else:
            status_line = "⚪ Остановлен"

        exchange_names = _get_exchange_names()
        text = f"⚡ <b>Арбитраж {exchange_names}</b>\n\n{status_line}"
        await _safe_edit(call.message, text, reply_markup=_main_keyboard(is_running))

    except Exception as e:
        logger.error(f"Menu error: {e}", exc_info=True)
