from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from stocks.system.config import StockExecutionConfig, StockRiskConfig, StockTradingConfig
from stocks.system.confirmation import SemiAutoConfirmationManager
from stocks.system.factory import (
    LiveStockMarketDataProvider,
    build_stock_engine,
)
from stocks.system.state import StockSystemState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine state singleton
# ---------------------------------------------------------------------------

@dataclass
class _StockEngineState:
    engine: Optional[Any] = None  # StockTradingEngine
    task: Optional[asyncio.Task] = None
    state: Optional[StockSystemState] = None
    provider: Optional[LiveStockMarketDataProvider] = None
    config: Optional[StockTradingConfig] = None
    confirmation: Optional[SemiAutoConfirmationManager] = None
    bcs_client: Optional[Any] = None
    ws: Optional[Any] = None
    ws_task: Optional[asyncio.Task] = None
    buffer_mgr: Optional[Any] = None
    user_id: Optional[int] = None
    last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def shutdown(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws_task and not self.ws_task.done():
            self.ws_task.cancel()
        if self.ws:
            await self.ws.close()
        if self.bcs_client:
            await self.bcs_client.close()
        self.engine = None
        self.task = None
        self.state = None
        logger.info("stock: shutdown complete")


_ses = _StockEngineState()
_bot_ref: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    global _bot_ref
    _bot_ref = bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe_edit(msg: types.Message, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


_MODE_LABELS = {
    "monitoring": "Мониторинг",
    "semi_auto": "Полуавто",
    "auto": "Авто",
}


def _stock_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    if _ses.running:
        rows.append([InlineKeyboardButton(text="\u23f9 Остановить", callback_data="stock_stop")])
        rows.append([
            InlineKeyboardButton(text="📊 Статистика", callback_data="stock_stats"),
            InlineKeyboardButton(text="📌 Позиции", callback_data="stock_positions"),
        ])
        # Mode switch row.
        mode = _ses.config.execution.mode if _ses.config else "monitoring"
        label = _MODE_LABELS.get(mode, mode)
        rows.append([InlineKeyboardButton(text=f"Режим: {label} \u27a1", callback_data="stock_mode_switch")])
        rows.append([InlineKeyboardButton(text="🚨 Закрыть все позиции", callback_data="stock_emergency")])
    else:
        rows.append([InlineKeyboardButton(text="\u25b6\ufe0f Запустить", callback_data="stock_start")])
    rows.append([InlineKeyboardButton(text="\u2699\ufe0f Настройки", callback_data="stock_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Semi-auto send/edit functions (used by SemiAutoConfirmationManager)
# ---------------------------------------------------------------------------

async def _send_confirmation(
    user_id: int, text: str, buttons: list
) -> Dict[str, Any]:
    if _bot_ref is None:
        return {}
    kb_rows = []
    for row in buttons:
        kb_rows.append([InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"]) for b in row])
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await _bot_ref.send_message(user_id, text, reply_markup=markup, parse_mode="HTML")
    return {"chat_id": msg.chat.id, "message_id": msg.message_id}


async def _edit_confirmation(chat_id: int, message_id: int, suffix: str) -> None:
    if _bot_ref is None:
        return
    try:
        await _bot_ref.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=suffix,
            parse_mode="HTML",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def handle_stocks_menu(message: types.Message) -> None:
    status = "🟢 работает" if _ses.running else "🔴 остановлен"
    mode = _MODE_LABELS.get(_ses.config.execution.mode, "—") if _ses.config else "—"
    text = (
        f"<b>🏦 Акции (MOEX через БКС)</b>\n"
        f"Статус: {status}\n"
        f"Режим: {mode}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_stock_menu_kb())


async def cb_stock_start(query: types.CallbackQuery) -> None:
    if _ses.running:
        await query.answer("Уже запущен")
        return

    try:
        config = StockTradingConfig.from_env()
        config.validate()
    except Exception as e:
        _ses.last_error = str(e)
        await query.answer(f"Ошибка конфигурации: {e}", show_alert=True)
        return

    try:
        # Создаём менеджер подтверждений для semi_auto и auto
        # (в auto режиме нужен, т.к. некоторые стратегии требуют подтверждения).
        confirmation = None
        if config.execution.mode in ("semi_auto", "auto"):
            confirmation = SemiAutoConfirmationManager(
                send_fn=_send_confirmation,
                edit_fn=_edit_confirmation,
                timeout_sec=config.execution.confirmation_timeout_sec,
            )

        engine, bcs_client, ws, tm, buffer_mgr, provider, state = build_stock_engine(
            config, confirmation
        )
        engine.user_id = query.from_user.id

        # Загрузка лотности и истории свечей.
        await provider.load_lot_sizes(config.tickers)
        await buffer_mgr.seed_from_rest(bcs_client, config.class_code, config.strategy.candle_timeframe)

        # Синхронизируем equity с реальным балансом из API.
        try:
            init_snap = await provider.get_snapshot(config.tickers[0])
            if init_snap.portfolio_value > 0:
                await state.set_equity(init_snap.portfolio_value, reset_peak=True)
                logger.info("stock: equity synced to %.2f RUB from API", init_snap.portfolio_value)
        except Exception:
            pass

        # Подписка WS на котировки + свечи.
        for ticker in config.tickers:
            ws.subscribe_quotes(ticker, config.class_code, provider.update_quote)
            ws.subscribe_candles(
                ticker, config.class_code, config.strategy.candle_timeframe,
                lambda candle, t=ticker: _on_candle(t, candle),
            )
        ws_task = asyncio.create_task(ws.connect())

        task = asyncio.create_task(engine.run_forever())

        _ses.engine = engine
        _ses.task = task
        _ses.state = state
        _ses.config = config
        _ses.provider = provider
        _ses.confirmation = confirmation
        _ses.bcs_client = bcs_client
        _ses.ws = ws
        _ses.ws_task = ws_task
        _ses.buffer_mgr = buffer_mgr
        _ses.user_id = query.from_user.id

        set_bot(query.bot)

        mode_label = _MODE_LABELS.get(config.execution.mode, config.execution.mode)
        dry = " (dry-run)" if config.execution.dry_run else ""
        await _safe_edit(
            query.message,
            f"<b>\u2705 Движок акций запущен</b>\n"
            f"Режим: {mode_label}{dry}\n"
            f"Тикеров: {len(config.tickers)}",
            _stock_menu_kb(),
        )
    except Exception as e:
        _ses.last_error = str(e)
        logger.error("stock: start failed: %s", e, exc_info=True)
        await query.answer(f"Ошибка запуска: {e}", show_alert=True)


async def _on_candle(ticker: str, candle) -> None:
    if _ses.buffer_mgr:
        _ses.buffer_mgr.on_candle_update(ticker, candle)


async def cb_stock_stop(query: types.CallbackQuery) -> None:
    await _ses.shutdown()
    await _safe_edit(query.message, "<b>\u23f9 Движок акций остановлен</b>", _stock_menu_kb())


async def cb_stock_stats(query: types.CallbackQuery) -> None:
    if not _ses.state or not _ses.provider or not _ses.config:
        await query.answer("Не запущен")
        return

    # Fetch real balance from API.
    try:
        real_snap = await _ses.provider.get_snapshot(_ses.config.tickers[0])
        portfolio_val = real_snap.portfolio_value
        cash_val = real_snap.cash_available
    except Exception:
        portfolio_val = 0
        cash_val = 0

    snap = await _ses.state.snapshot()
    dd = await _ses.state.drawdowns()
    mode_label = _MODE_LABELS.get(_ses.config.execution.mode, "—")
    dry = " (dry-run)" if _ses.config.execution.dry_run else ""

    lines = [
        f"<b>📊 Статистика акций</b>",
        f"Режим: {mode_label}{dry}",
        f"",
        f"<b>Реальный баланс (БКС):</b>",
        f"  Портфель: {portfolio_val:,.0f} \u20bd",
        f"  Свободные: {cash_val:,.0f} \u20bd",
    ]

    if _ses.config.execution.dry_run:
        sim_pnl = snap.get("simulated_pnl", 0)
        sim_daily = snap.get("daily_simulated_pnl", 0)
        lines += [
            f"",
            f"<b>Симуляция (dry-run):</b>",
            f"  P&L: {sim_pnl:+,.1f} \u20bd",
            f"  P&L за день: {sim_daily:+,.1f} \u20bd",
        ]
    else:
        lines += [
            f"",
            f"<b>Торговля:</b>",
            f"  P&L: {snap['realized_pnl']:+,.1f} \u20bd",
            f"  P&L за день: {snap['daily_realized_pnl']:+,.1f} \u20bd",
        ]

    lines += [
        f"",
        f"Открытых позиций: {snap['open_positions']}",
        f"Сделок за день: {snap['daily_trade_count']}",
        f"Kill-switch: {'ВКЛ' if snap['kill_switch'] else 'выкл'}",
    ]

    await _safe_edit(query.message, "\n".join(lines), _stock_menu_kb())


async def cb_stock_positions(query: types.CallbackQuery) -> None:
    if not _ses.state:
        await query.answer("Не запущен")
        return
    positions = await _ses.state.list_positions()
    if not positions:
        await _safe_edit(query.message, "<b>Нет открытых позиций</b>", _stock_menu_kb())
        return
    lines = ["<b>📌 Открытые позиции</b>"]
    for p in positions:
        age_min = int((time.time() - p.opened_at) / 60)
        side_ru = "ПОКУПКА" if p.side == "buy" else "ПРОДАЖА"
        trail_info = f" trail={p.trailing_stop_pct:.1f}%" if p.trailing_stop_pct > 0 else ""
        lines.append(
            f"  {side_ru} {p.ticker} x{p.quantity_lots} @ {p.entry_price:.2f}\u20bd\n"
            f"    SL={p.stop_loss_price:.2f} TP={p.take_profit_price:.2f}{trail_info}\n"
            f"    [{p.strategy_id.value}] {age_min} мин"
        )
    await _safe_edit(query.message, "\n".join(lines), _stock_menu_kb())


async def cb_stock_signals(query: types.CallbackQuery) -> None:
    await query.answer("Сигналы выводятся в лог в режиме мониторинга")


async def cb_stock_settings(query: types.CallbackQuery) -> None:
    if _ses.config:
        cfg = _ses.config
    else:
        try:
            cfg = StockTradingConfig.from_env()
        except Exception as e:
            await query.answer(f"Ошибка: {e}", show_alert=True)
            return

    mode_label = _MODE_LABELS.get(cfg.execution.mode, cfg.execution.mode)
    text = (
        f"<b>\u2699\ufe0f Настройки акций</b>\n"
        f"Режим: {mode_label}\n"
        f"Dry-run: {'да' if cfg.execution.dry_run else 'нет'}\n"
        f"Тикеры: {len(cfg.tickers)} шт.\n"
        f"Стратегии: {', '.join(cfg.strategy.enabled)}\n"
        f"Таймфрейм: {cfg.strategy.candle_timeframe}\n"
        f"\n<b>Риск-лимиты:</b>\n"
        f"  Макс. экспозиция: {cfg.risk.max_total_exposure_pct:.0%} от баланса\n"
        f"  Макс. на позицию: {cfg.risk.max_per_position_pct:.0%} от баланса\n"
        f"  Макс. позиций: {cfg.risk.max_open_positions}\n"
        f"  Макс. сделок/день: {cfg.risk.max_daily_trades}\n"
        f"\n<b>Выход из позиции:</b>\n"
        f"  Stop-loss: {cfg.risk.default_sl_pct:.1f}%\n"
        f"  Take-profit: {cfg.risk.default_tp_pct:.1f}%\n"
        f"  Trailing stop: {cfg.risk.trailing_stop_pct:.1f}%\n"
        f"\n<b>Фильтры качества:</b>\n"
        f"  Мин. уверенность: {cfg.risk.min_confidence:.0%}\n"
        f"  Мин. доходность: {cfg.risk.min_edge_pct:.2f}%\n"
        f"  Кулдаун сигнала: {cfg.risk.signal_cooldown_sec // 60} мин\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"Экспозиция: {cfg.risk.max_total_exposure_pct:.0%}", callback_data="stock_set_exposure"),
            InlineKeyboardButton(text=f"На позицию: {cfg.risk.max_per_position_pct:.0%}", callback_data="stock_set_per_pos"),
        ],
        [
            InlineKeyboardButton(text=f"Позиций: {cfg.risk.max_open_positions}", callback_data="stock_set_max_pos"),
            InlineKeyboardButton(text=f"Сделок/день: {cfg.risk.max_daily_trades}", callback_data="stock_set_max_trades"),
        ],
        [
            InlineKeyboardButton(text=f"SL: {cfg.risk.default_sl_pct:.1f}%", callback_data="stock_set_sl"),
            InlineKeyboardButton(text=f"TP: {cfg.risk.default_tp_pct:.1f}%", callback_data="stock_set_tp"),
            InlineKeyboardButton(text=f"Trail: {cfg.risk.trailing_stop_pct:.1f}%", callback_data="stock_set_trail"),
        ],
        [
            InlineKeyboardButton(text=f"Мин.увер: {cfg.risk.min_confidence:.0%}", callback_data="stock_set_min_conf"),
            InlineKeyboardButton(text=f"Мин.доход: {cfg.risk.min_edge_pct:.2f}%", callback_data="stock_set_min_edge"),
            InlineKeyboardButton(text=f"Кулдаун: {cfg.risk.signal_cooldown_sec // 60}м", callback_data="stock_set_cooldown"),
        ],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_menu")],
    ])
    await _safe_edit(query.message, text, kb)


def _rebuild_config_with_risk(new_risk: StockRiskConfig) -> None:
    """Rebuild frozen config with new risk settings (in-place on _ses)."""
    old = _ses.config
    new_config = StockTradingConfig(
        tickers=old.tickers,
        class_code=old.class_code,
        credentials=old.credentials,
        starting_equity=old.starting_equity,
        risk=new_risk,
        execution=old.execution,
        strategy=old.strategy,
    )
    _ses.config = new_config
    if _ses.engine:
        _ses.engine.config = new_config
        _ses.engine.risk.config = new_risk
        _ses.engine.execution.risk_config = new_risk


_EXPOSURE_OPTIONS = [0.25, 0.50, 0.75, 1.0]
_PER_POS_OPTIONS = [0.10, 0.25, 0.50, 1.0]
_MAX_POS_OPTIONS = [1, 3, 5, 10, 20]
_MAX_TRADES_OPTIONS = [3, 5, 8, 15, 30]
_SL_OPTIONS = [2.0, 3.0, 4.0, 5.0, 7.0]
_TP_OPTIONS = [3.0, 4.5, 6.0, 8.0, 10.0]
_TRAIL_OPTIONS = [0.0, 1.0, 1.5, 2.0, 3.0]
_MIN_CONF_OPTIONS = [0.10, 0.15, 0.20, 0.30, 0.40]
_MIN_EDGE_OPTIONS = [0.05, 0.10, 0.15, 0.25, 0.40]
_COOLDOWN_OPTIONS = [120, 300, 600, 900, 1800]  # seconds


def _clone_risk(**overrides) -> StockRiskConfig:
    """Clone current risk config with overrides."""
    import dataclasses
    old = _ses.config.risk
    fields = {f.name: getattr(old, f.name) for f in dataclasses.fields(old)}
    fields.update(overrides)
    return StockRiskConfig(**fields)


async def cb_stock_set_exposure(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{int(v*100)}%", callback_data=f"stock_exposure_val:{v}") for v in _EXPOSURE_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Макс. экспозиция</b>\nДоля свободных средств в открытых позициях:", kb)


async def cb_stock_set_per_pos(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{int(v*100)}%", callback_data=f"stock_perpos_val:{v}") for v in _PER_POS_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Макс. на одну позицию</b>\nДоля баланса на одну сделку:", kb)


async def cb_stock_set_max_pos(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(v), callback_data=f"stock_maxpos_val:{v}") for v in _MAX_POS_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Макс. открытых позиций</b>\nОдновременно:", kb)


async def cb_stock_set_max_trades(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(v), callback_data=f"stock_maxtrades_val:{v}") for v in _MAX_TRADES_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Макс. сделок за день</b>:", kb)


async def cb_stock_set_sl(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v}%", callback_data=f"stock_sl_val:{v}") for v in _SL_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Stop-Loss</b>\nМаксимальный убыток на позицию (%):", kb)


async def cb_stock_set_tp(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v}%", callback_data=f"stock_tp_val:{v}") for v in _TP_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Take-Profit</b>\nЦелевая прибыль на позицию (%):", kb)


async def cb_stock_set_trail(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("Выкл" if v == 0 else f"{v}%"), callback_data=f"stock_trail_val:{v}") for v in _TRAIL_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Trailing Stop</b>\nSL подтягивается за ценой на это расстояние (%).\n0 = выключен:", kb)


async def cb_stock_exposure_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(max_total_exposure_pct=val))
    await query.answer(f"Экспозиция: {int(val*100)}%")
    await cb_stock_settings(query)


async def cb_stock_perpos_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(max_per_position_pct=val))
    await query.answer(f"На позицию: {int(val*100)}%")
    await cb_stock_settings(query)


async def cb_stock_maxpos_val(query: types.CallbackQuery) -> None:
    val = int(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(max_open_positions=val))
    await query.answer(f"Макс. позиций: {val}")
    await cb_stock_settings(query)


async def cb_stock_maxtrades_val(query: types.CallbackQuery) -> None:
    val = int(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(max_daily_trades=val))
    await query.answer(f"Макс. сделок: {val}")
    await cb_stock_settings(query)


async def cb_stock_sl_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(default_sl_pct=val))
    await query.answer(f"Stop-Loss: {val}%")
    await cb_stock_settings(query)


async def cb_stock_tp_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(default_tp_pct=val))
    await query.answer(f"Take-Profit: {val}%")
    await cb_stock_settings(query)


async def cb_stock_trail_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(trailing_stop_pct=val))
    await query.answer(f"Trailing Stop: {'выкл' if val == 0 else f'{val}%'}")
    await cb_stock_settings(query)


async def cb_stock_set_min_conf(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{int(v*100)}%", callback_data=f"stock_minconf_val:{v}") for v in _MIN_CONF_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Мин. уверенность</b>\nСигналы с уверенностью ниже этого порога отклоняются:", kb)


async def cb_stock_set_min_edge(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v}%", callback_data=f"stock_minedge_val:{v}") for v in _MIN_EDGE_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Мин. ожидаемая доходность</b>\nДолжна превышать комиссию БКС (~0.1% за сделку):", kb)


async def cb_stock_set_cooldown(query: types.CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v // 60} мин", callback_data=f"stock_cooldown_val:{v}") for v in _COOLDOWN_OPTIONS],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="stock_settings")],
    ])
    await _safe_edit(query.message, "<b>Кулдаун сигнала</b>\nПауза между повторными сигналами одной стратегии:", kb)


async def cb_stock_minconf_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(min_confidence=val))
    await query.answer(f"Мин. уверенность: {int(val*100)}%")
    await cb_stock_settings(query)


async def cb_stock_minedge_val(query: types.CallbackQuery) -> None:
    val = float(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(min_edge_pct=val))
    await query.answer(f"Мин. доходность: {val}%")
    await cb_stock_settings(query)


async def cb_stock_cooldown_val(query: types.CallbackQuery) -> None:
    val = int(query.data.split(":")[1])
    _rebuild_config_with_risk(_clone_risk(signal_cooldown_sec=val))
    await query.answer(f"Кулдаун: {val // 60} мин")
    await cb_stock_settings(query)


async def cb_stock_mode_switch(query: types.CallbackQuery) -> None:
    """Переключение: мониторинг → полуавто → авто → мониторинг."""
    if not _ses.running or not _ses.config:
        await query.answer("Не запущен")
        return

    cycle = ["monitoring", "semi_auto", "auto"]
    current = _ses.config.execution.mode
    idx = cycle.index(current) if current in cycle else 0
    new_mode = cycle[(idx + 1) % len(cycle)]

    # Пересобираем frozen dataclass с новым режимом.
    # В semi_auto и auto режимах dry_run выключается — сделки реальные.
    old_exec = _ses.config.execution
    new_dry_run = old_exec.dry_run
    if new_mode in ("semi_auto", "auto"):
        new_dry_run = False
    elif new_mode == "monitoring":
        new_dry_run = True
    new_exec = StockExecutionConfig(
        order_timeout_ms=old_exec.order_timeout_ms,
        cycle_interval_seconds=old_exec.cycle_interval_seconds,
        mode=new_mode,
        confirmation_timeout_sec=old_exec.confirmation_timeout_sec,
        dry_run=new_dry_run,
    )
    new_config = StockTradingConfig(
        tickers=_ses.config.tickers,
        class_code=_ses.config.class_code,
        credentials=_ses.config.credentials,
        starting_equity=_ses.config.starting_equity,
        risk=_ses.config.risk,
        execution=new_exec,
        strategy=_ses.config.strategy,
    )
    _ses.config = new_config
    _ses.engine.config = new_config
    _ses.engine.execution.config = new_exec  # Sync dry_run flag to execution engine.

    # Создаём менеджер подтверждений если нужен.
    if new_mode in ("semi_auto", "auto") and not _ses.confirmation:
        _ses.confirmation = SemiAutoConfirmationManager(
            send_fn=_send_confirmation,
            edit_fn=_edit_confirmation,
            timeout_sec=new_config.execution.confirmation_timeout_sec,
        )
        _ses.engine.confirmation = _ses.confirmation

    label = _MODE_LABELS.get(new_mode, new_mode)
    logger.info("stock: mode switched to %s (dry_run=%s)", new_mode, new_dry_run)
    await query.answer(f"Режим: {label}")
    await handle_stocks_menu_inline(query)


async def cb_stock_menu(query: types.CallbackQuery) -> None:
    await handle_stocks_menu_inline(query)


async def handle_stocks_menu_inline(query: types.CallbackQuery) -> None:
    status = "🟢 работает" if _ses.running else "🔴 остановлен"
    mode = _MODE_LABELS.get(_ses.config.execution.mode, "—") if _ses.config else "—"
    text = f"<b>🏦 Акции (MOEX через БКС)</b>\nСтатус: {status}\nРежим: {mode}"
    await _safe_edit(query.message, text, _stock_menu_kb())


# ---------------------------------------------------------------------------
# Semi-auto confirmation callbacks
# ---------------------------------------------------------------------------

async def cb_stock_confirm(query: types.CallbackQuery) -> None:
    if not _ses.confirmation:
        await query.answer("Нет ожидающих подтверждений")
        return
    intent_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    if _ses.confirmation.on_confirm(intent_id):
        await query.answer("Подтверждено!")
        try:
            old_text = query.message.text or ""
            await query.message.edit_text(
                f"{old_text}\n\n\u2705 <b>ПОДТВЕРЖДЕНО</b> — исполняется...",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        await query.answer("Истекло или не найдено")


async def cb_stock_reject(query: types.CallbackQuery) -> None:
    if not _ses.confirmation:
        await query.answer("Нет ожидающих подтверждений")
        return
    intent_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    if _ses.confirmation.on_reject(intent_id):
        await query.answer("Отклонено")
        try:
            old_text = query.message.text or ""
            await query.message.edit_text(
                f"{old_text}\n\n\u274c <b>ОТКЛОНЕНО</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        await query.answer("Истекло или не найдено")


# ---------------------------------------------------------------------------
# Emergency close
# ---------------------------------------------------------------------------

async def cb_stock_emergency(query: types.CallbackQuery) -> None:
    if not _ses.state or not _ses.engine:
        await query.answer("Не запущен")
        return
    positions = await _ses.state.list_positions()
    if not positions:
        await query.answer("Нет открытых позиций")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="\u2705 Подтвердить закрытие", callback_data="stock_emergency_confirm"),
            InlineKeyboardButton(text="\u274c Отмена", callback_data="stock_menu"),
        ]
    ])
    await _safe_edit(
        query.message,
        f"<b>🚨 Закрыть ВСЕ {len(positions)} позиций?</b>",
        kb,
    )


async def cb_stock_emergency_confirm(query: types.CallbackQuery) -> None:
    if not _ses.state or not _ses.engine:
        await query.answer("Не запущен")
        return
    positions = await _ses.state.list_positions()
    closed = 0
    for pos in positions:
        try:
            snapshot = await _ses.provider.get_snapshot(pos.ticker)
            await _ses.engine.execution.execute_exit(pos, "emergency", snapshot.quote.last)
            closed += 1
        except Exception as exc:
            logger.error("stock: emergency close failed for %s: %s", pos.position_id, exc)
    await _safe_edit(
        query.message,
        f"<b>🚨 Экстренное закрытие: {closed}/{len(positions)}</b>",
        _stock_menu_kb(),
    )


# ---------------------------------------------------------------------------
# Shutdown hook
# ---------------------------------------------------------------------------

async def shutdown_stocks() -> None:
    await _ses.shutdown()
