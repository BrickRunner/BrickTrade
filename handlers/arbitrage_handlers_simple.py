from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from dataclasses import replace
from typing import Optional

from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from arbitrage.core.market_data import MarketDataEngine
from arbitrage.system import (
    AtomicExecutionEngine,
    InMemoryMonitoring,
    LiveExecutionVenue,
    LiveMarketDataProvider,
    SlippageModel,
    SystemState,
    TradingSystemConfig,
    TradingSystemEngine,
    build_exchange_clients,
    usdt_symbol_universe,
)
from arbitrage.system.factory import build_private_ws_manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine state encapsulated in a single object
# ---------------------------------------------------------------------------


@dataclass
class _EngineState:
    engine: Optional[TradingSystemEngine] = None
    task: Optional[asyncio.Task] = None
    state: Optional[SystemState] = None
    provider: Optional[LiveMarketDataProvider] = None
    venue: Optional[LiveExecutionVenue] = None
    monitor: Optional[InMemoryMonitoring] = None
    config: Optional[TradingSystemConfig] = None
    exchanges: dict = field(default_factory=dict)
    user_id: Optional[int] = None
    last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def exchange_names(self) -> str:
        if self.config:
            return "/".join(ex.upper() for ex in self.config.exchanges)
        if self.exchanges:
            return "/".join(ex.upper() for ex in self.exchanges.keys())
        return "OKX/HTX"

    def task_state_str(self) -> str:
        if self.task is None:
            return "stopped"
        if not self.task.done():
            return "running"
        if self.task.cancelled():
            return "cancelled"
        try:
            self.task.exception()
            return "failed"
        except Exception:
            return "done"

    async def shutdown(self) -> None:
        task = self.task
        self.task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.venue:
            # Stop private WS connections first.
            if hasattr(self.venue, "private_ws") and self.venue.private_ws:
                try:
                    await self.venue.private_ws.stop()
                except Exception:
                    pass
            try:
                await self.venue.close()
            except Exception:
                pass
        self.engine = None
        self.state = None
        self.provider = None
        self.venue = None
        self.monitor = None
        self.config = None
        self.exchanges = {}
        self.user_id = None
        self.last_error = None


_es = _EngineState()

# Compatibility aliases used by main.py shutdown block.
_router = None
_router_task: Optional[asyncio.Task] = None
_state: Optional[SystemState] = None
_exchanges: dict = {}


# ---------------------------------------------------------------------------
# Telegram monitoring sink
# ---------------------------------------------------------------------------


class TelegramMonitoringSink(InMemoryMonitoring):
    def __init__(self, tg_logger: logging.Logger, bot, user_id: int):
        super().__init__(logger=tg_logger)
        self._bot = bot
        self._user_id = user_id

    async def emit(self, event: str, payload: dict) -> None:
        await super().emit(event, payload)
        text = self._format_event_message(event, payload)
        if not text:
            return
        try:
            await self._bot.send_message(self._user_id, text, parse_mode="HTML")
        except Exception:
            logger.debug("telegram_notify_failed event=%s", event, exc_info=True)

    @staticmethod
    def _format_event_message(event: str, payload: dict) -> Optional[str]:
        if event == "execution_fill" and not payload.get("dry_run", True):
            long_px = payload.get('entry_long_price', 0)
            short_px = payload.get('entry_short_price', 0)
            spread_pct = 0.0
            if long_px and short_px and long_px > 0:
                spread_pct = (short_px - long_px) / long_px * 100
            return (
                "✅ <b>Позиция открыта</b>\n\n"
                f"📍 Пара: <b>{payload.get('symbol')}</b>\n"
                f"📈 Long: <b>{str(payload.get('long_exchange', '')).upper()}</b> @ <code>{long_px:.4f}</code>\n"
                f"📉 Short: <b>{str(payload.get('short_exchange', '')).upper()}</b> @ <code>{short_px:.4f}</code>\n"
                f"📊 Спред: <code>{spread_pct:.4f}%</code>\n"
                f"🆔 <code>{payload.get('position_id', '')[:8]}</code>"
            )
        if event == "execution_fill" and payload.get("dry_run", True):
            return (
                "📝 <b>DRY RUN — позиция открыта</b>\n"
                f"📍 <b>{payload.get('symbol')}</b> | {payload.get('strategy')}\n"
                f"💰 Notional: <code>${payload.get('notional', 0):.2f}</code>"
            )
        if event == "position_close_signal":
            pnl = payload.get('pnl_usd', 0)
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            reason_map = {
                "take_profit": "✅ Тейк-профит",
                "stop_loss": "🛑 Стоп-лосс",
                "max_holding_time": "⏰ Макс. время",
                "edge_converged": "📊 Спред сошёлся",
                "per_trade_max_loss": "🚨 Макс. убыток",
                "funding_reversed": "💸 Фандинг развернулся",
            }
            reason = reason_map.get(payload.get('reason', ''), payload.get('reason', ''))
            age = int(payload.get('age_sec', 0))
            age_str = f"{age // 60}м {age % 60}с" if age >= 60 else f"{age}с"
            return (
                f"🔎 <b>Закрытие позиции</b>\n\n"
                f"📍 <b>{payload.get('symbol')}</b>\n"
                f"Причина: {reason}\n"
                f"{pnl_emoji} PnL: <code>{pnl:.4f} USDT</code>\n"
                f"⏱ Удержание: {age_str}"
            )
        if event == "position_closed":
            pnl = payload.get('realized_pnl_usd', 0)
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_sign = "+" if pnl > 0 else ""
            reason_map = {
                "take_profit": "✅ Тейк-профит",
                "stop_loss": "🛑 Стоп-лосс",
                "max_holding_time": "⏰ Макс. время",
                "edge_converged": "📊 Спред сошёлся",
                "per_trade_max_loss": "🚨 Макс. убыток",
                "funding_reversed": "💸 Фандинг развернулся",
            }
            reason = reason_map.get(payload.get('reason', ''), payload.get('reason', ''))
            return (
                f"🧾 <b>Позиция закрыта</b>\n\n"
                f"📍 <b>{payload.get('symbol')}</b>\n"
                f"Причина: {reason}\n"
                f"{pnl_emoji} Realized PnL: <b>{pnl_sign}{pnl:.4f} USDT</b>\n"
                f"🆔 <code>{str(payload.get('position_id', ''))[:8]}</code>"
            )
        if event == "execution_critical":
            return (
                "🚨🚨 <b>КРИТИЧЕСКАЯ ОШИБКА</b>\n\n"
                f"📍 <b>{payload.get('symbol')}</b>\n"
                f"Стратегия: {payload.get('strategy')}\n"
                f"Причина: <code>{payload.get('reason')}</code>\n\n"
                "⛔ Kill-switch активирован.\n"
                "Торговля остановлена до ручного сброса."
            )
        if event == "symbol_cooldown":
            cd_sec = int(payload.get("cooldown_seconds", 0) or 0)
            cd_hours = cd_sec / 3600
            return (
                "⛔ <b>Пара временно отключена</b>\n\n"
                f"📍 <b>{payload.get('symbol')}</b>\n"
                f"Причина: серия из {payload.get('loss_streak', 0)} убытков подряд\n"
                f"⏱ Блокировка: {cd_hours:.1f} ч"
            )
        if event == "per_trade_max_loss":
            return (
                "🚨 <b>Макс. убыток по сделке</b>\n\n"
                f"PnL: <code>{payload.get('pnl_usd', 0):.4f} USDT</code>\n"
                f"Лимит: <code>{payload.get('limit_usd', 0):.4f} USDT</code>\n"
                "⛔ Kill-switch активирован."
            )
        if event == "execution_hedge":
            if not payload.get("hedged"):
                return (
                    "⚠️ <b>Хедж НЕ УДАЛСЯ</b>\n\n"
                    f"📍 <b>{payload.get('position_symbol')}</b>\n"
                    f"Биржа: {str(payload.get('first_leg_exchange', '')).upper()}\n"
                    "Требуется ручная проверка позиций!"
                )
            return None
        return None


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


async def _safe_edit(message: types.Message, text: str, reply_markup=None, parse_mode: str = "HTML") -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


def _main_keyboard(is_running: bool) -> InlineKeyboardMarkup:
    if not is_running:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="▶️ Старт", callback_data="arb_multi_start")]]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Возможности", callback_data="arb_scan_now"),
             InlineKeyboardButton(text="📈 Статистика", callback_data="arb_stats")],
            [InlineKeyboardButton(text="📋 История", callback_data="arb_history"),
             InlineKeyboardButton(text="💸 Funding", callback_data="arb_funding")],
            [InlineKeyboardButton(text="📉 Basis", callback_data="arb_basis"),
             InlineKeyboardButton(text="📊 Stat Arb", callback_data="arb_stat_arb")],
            [InlineKeyboardButton(text="🚨 Закрыть всё", callback_data="arb_emergency_close")],
            [InlineKeyboardButton(text="⏹ Стоп", callback_data="arb_multi_stop")],
        ]
    )


# ---------------------------------------------------------------------------
# Affordability helpers
# ---------------------------------------------------------------------------


def _symbol_min_notional_usd(market_data: MarketDataEngine, exchange: str, symbol: str) -> float:
    ticker = market_data.get_futures_price(exchange, symbol)
    if not ticker:
        return 0.0
    mid = (ticker.bid + ticker.ask) / 2
    ct = market_data.get_contract_size(exchange, symbol)
    if mid <= 0 or ct <= 0:
        return 0.0
    if exchange == "bybit":
        return 5.0
    return mid * ct


def _filter_affordable_symbols(
    market_data: MarketDataEngine,
    symbols: list[str],
    exchanges: list[str],
    balances: dict[str, float],
    headroom: float = 1.0,
) -> list[str]:
    affordable: list[str] = []
    for symbol in symbols:
        ok = True
        for exchange in exchanges:
            available = max(0.0, balances.get(exchange, 0.0)) * max(0.0, min(1.0, headroom))
            min_notional = _symbol_min_notional_usd(market_data, exchange, symbol)
            if min_notional <= 0 or available < min_notional:
                ok = False
                break
        if ok:
            affordable.append(symbol)
    return affordable


def _best_joint_requirement_hint(
    market_data: MarketDataEngine,
    symbols: list[str],
    exchanges: list[str],
    headroom: float = 1.0,
) -> tuple[str, dict[str, float]] | tuple[None, None]:
    best_symbol = None
    best_requirements: dict[str, float] | None = None
    best_score = None
    scale = max(0.01, min(1.0, headroom))
    for symbol in symbols:
        req: dict[str, float] = {}
        valid = True
        for exchange in exchanges:
            mn = _symbol_min_notional_usd(market_data, exchange, symbol)
            if mn <= 0:
                valid = False
                break
            req[exchange] = mn / scale
        if not valid:
            continue
        score = max(req.values()) if req else float("inf")
        if best_score is None or score < best_score:
            best_score = score
            best_symbol = symbol
            best_requirements = req
    if best_symbol is None or best_requirements is None:
        return None, None
    return best_symbol, best_requirements


def _min_required_balance_hint(
    market_data: MarketDataEngine,
    symbols: list[str],
    exchanges: list[str],
    headroom: float = 1.0,
) -> dict[str, float]:
    hint: dict[str, float] = {}
    for exchange in exchanges:
        mins = []
        for symbol in symbols:
            mn = _symbol_min_notional_usd(market_data, exchange, symbol)
            if mn > 0:
                mins.append(mn / max(0.01, min(1.0, headroom)))
        if mins:
            hint[exchange] = min(mins)
    return hint


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _build_status_line() -> str:
    if not _es.state or not _es.config:
        return "⚪ Остановлен"
    err = f"\nОшибка: {_es.last_error}" if _es.last_error else ""
    return (
        f"🟢 {'DRY_RUN' if _es.config.execution.dry_run else 'LIVE'}\n"
        f"Task: {_es.task_state_str()}\n"
        f"Биржи: {_es.exchange_names()}\n"
        f"Стратегии: {', '.join(_es.config.strategy.enabled)}\n"
        f"Символов: {len(_es.config.symbols)}{err}"
    )


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------


async def _run_engine_task() -> None:
    try:
        await _es.engine.run_forever()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _es.last_error = str(exc)
        logger.error("engine_fatal_error: %s", exc, exc_info=True)
        if _es.user_id and _es.monitor:
            try:
                await _es.monitor.emit("engine_fatal_error", {"error": str(exc)})
            except Exception:
                pass


async def _start_engine(bot, user_id: int) -> str:
    global _router, _router_task, _state, _exchanges
    if _es.running:
        return "Уже запущен"

    clients = None
    venue = None
    try:
        config = TradingSystemConfig.from_env()
        config.validate()
        clients = build_exchange_clients(config)
        market_data = MarketDataEngine(clients)
        provider = LiveMarketDataProvider(market_data=market_data, exchanges=config.exchanges)
        await provider.initialize()

        if config.trade_all_symbols:
            selected = usdt_symbol_universe(market_data, config.max_symbols, config.symbol_blacklist)
            config = replace(config, symbols=selected)

        state = SystemState(starting_equity=config.starting_equity)
        monitor = TelegramMonitoringSink(tg_logger=logging.getLogger("trading_system"), bot=bot, user_id=user_id)
        venue = LiveExecutionVenue(exchanges=clients, market_data=market_data)
        # Start private WebSocket connections for real-time balance/fill/position updates.
        private_ws = build_private_ws_manager(config)
        await private_ws.start()
        await private_ws.seed_balances(market_data)
        venue.private_ws = private_ws
        provider.private_ws = private_ws

        # Align working equity with real available balances to avoid oversizing.
        # Use WS-seeded balances (more reliable than raw REST for HTX).
        balances = private_ws.get_all_balances()
        if not balances:
            balances = await market_data.fetch_balances()
        live_equity = sum(max(0.0, v) for v in balances.values())
        if live_equity > 0:
            config = replace(config, starting_equity=live_equity)
            await state.set_equity(live_equity)

        # Prioritize affordable symbols (put them first), but keep ALL symbols
        # so the engine can monitor spreads and trade when margin allows.
        affordable = _filter_affordable_symbols(
            market_data, config.symbols, config.exchanges, balances, headroom=1.0
        )
        if affordable:
            # Put affordable symbols first for priority scanning
            remaining = [s for s in config.symbols if s not in affordable]
            config = replace(config, symbols=affordable + remaining)
            logger.info(
                "Symbol priority: %d affordable first, %d total symbols",
                len(affordable), len(config.symbols),
            )
        else:
            logger.warning(
                "No symbols currently affordable (balances: %s), "
                "keeping all %d symbols — engine will filter by margin at execution time.",
                {ex: f"${max(0.0, balances.get(ex, 0.0)):.2f}" for ex in config.exchanges},
                len(config.symbols),
            )
        execution = AtomicExecutionEngine(
            config=config.execution,
            venue=venue,
            slippage=SlippageModel(),
            state=state,
            monitor=monitor,
        )
        engine = TradingSystemEngine.create(
            config=config,
            provider=provider,
            monitor=monitor,
            execution=execution,
            state=state,
        )
    except Exception:
        if venue is not None:
            try:
                await venue.close()
            except Exception:
                pass
        elif clients:
            for client in clients.values():
                if hasattr(client, "close"):
                    try:
                        await client.close()
                    except Exception:
                        pass
        raise

    _es.config = config
    _es.exchanges = clients
    _es.provider = provider
    _es.venue = venue
    _es.monitor = monitor
    _es.state = state
    _es.engine = engine
    _es.user_id = user_id
    _es.last_error = None
    _es.task = asyncio.create_task(_run_engine_task())

    # Sync compatibility aliases
    _router_task = _es.task
    _state = _es.state
    _exchanges = _es.exchanges
    _router = _es.engine

    await bot.send_message(
        user_id,
        (
            "✅ <b>Новый trading engine запущен</b>\n\n"
            f"Режим: <b>{'DRY_RUN' if config.execution.dry_run else 'LIVE'}</b>\n"
            f"Биржи: <b>{_es.exchange_names()}</b>\n"
            f"Символов: {len(config.symbols)}\n"
            f"Стратегии: {', '.join(config.strategy.enabled)}\n"
            f"Equity: ${config.starting_equity:.2f}"
        ),
        parse_mode="HTML",
    )
    return "Запущен"


async def shutdown_arbitrage() -> None:
    global _router, _router_task, _state, _exchanges
    await _es.shutdown()
    _router_task = None
    _router = None
    _state = None
    _exchanges = {}


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


async def handle_arbitrage_menu(m: types.Message):
    is_running = _es.running
    text = f"⚡ <b>Арбитраж {_es.exchange_names()}</b>\n\n{_build_status_line()}"
    await m.answer(text, reply_markup=_main_keyboard(is_running), parse_mode="HTML")


async def cb_arb_multi_start(call: types.CallbackQuery):
    try:
        if _es.running:
            await call.answer("Уже запущен")
            return
        await call.answer()
        await _safe_edit(call.message, "⏳ <b>Запуск нового engine...</b>")
        status = await _start_engine(call.bot, call.from_user.id)
        await _safe_edit(call.message, f"🟢 <b>{status}</b>\n\n{_build_status_line()}", reply_markup=_main_keyboard(True))
    except Exception as exc:
        logger.error("Start error: %s", exc, exc_info=True)
        await _safe_edit(call.message, f"❌ {exc}", reply_markup=_main_keyboard(False))


async def cb_arb_multi_stop(call: types.CallbackQuery):
    await call.answer()
    await shutdown_arbitrage()
    await _safe_edit(call.message, "⏹ <b>Остановлен</b>", reply_markup=_main_keyboard(False))


async def cb_arb_scan_now(call: types.CallbackQuery):
    await call.answer("Сканирую...")
    if not _es.engine or not _es.provider:
        await _safe_edit(call.message, "📊 Бот не запущен", reply_markup=_main_keyboard(False))
        return
    lines = ["📊 <b>Возможности арбитража</b>", ""]
    found = 0
    for symbol in _es.config.symbols[:15]:
        try:
            snapshot = await _es.provider.get_snapshot(symbol)
            intents = await _es.engine.strategies.generate_intents(snapshot)
            intents.sort(key=lambda x: x.expected_edge_bps * x.confidence, reverse=True)
            for intent in intents[:2]:
                found += 1
                meta = intent.metadata
                arb_type = meta.get("arb_type", "spread")
                net_spread = meta.get("net_spread_pct", 0)
                raw_spread = meta.get("spread_pct", meta.get("funding_rate_diff_pct", 0))
                fees = meta.get("total_fees_pct", 0)
                profitable = "✅" if net_spread > 0 else "⚠️"

                if arb_type == "funding_rate":
                    lines.append(
                        f"{profitable} <b>{symbol}</b> — funding arb\n"
                        f"  Long {intent.long_exchange.upper()} ← Short {intent.short_exchange.upper()}\n"
                        f"  FR diff: <code>{raw_spread:.4f}%</code> | Комиссии: <code>{fees:.4f}%</code>\n"
                        f"  Edge: <code>{intent.expected_edge_bps:.1f} bps</code> | Уверенность: <code>{intent.confidence:.0%}</code>"
                    )
                else:
                    lines.append(
                        f"{profitable} <b>{symbol}</b> — спред арбитраж\n"
                        f"  Buy {intent.long_exchange.upper()} @ <code>{meta.get('long_price', 0):.4f}</code>\n"
                        f"  Sell {intent.short_exchange.upper()} @ <code>{meta.get('short_price', 0):.4f}</code>\n"
                        f"  Спред: <code>{raw_spread:.4f}%</code> | Нетто: <code>{net_spread:.4f}%</code> | Комиссии: <code>{fees:.4f}%</code>"
                    )
                lines.append("")
        except Exception:
            continue
        if found >= 10:
            break
    if found == 0:
        lines.append("Нет валидных сигналов в текущем цикле.\n"
                      "Минимальный спред для входа: "
                      f"<code>{_es.config.strategy.min_spread_pct:.2f}%</code> (после комиссий)")
    await _safe_edit(
        call.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_scan_now")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
            ]
        ),
    )


async def cb_arb_stats(call: types.CallbackQuery):
    await call.answer("Статистика...")
    if not _es.state or not _es.config:
        await _safe_edit(call.message, "📈 Бот не запущен", reply_markup=_main_keyboard(False))
        return
    snap = await _es.state.snapshot()
    dd = await _es.state.drawdowns()
    positions = await _es.state.list_positions()

    mode_label = "🟡 DRY RUN" if _es.config.execution.dry_run else "🟢 LIVE"
    pnl_emoji = "🟢" if snap['realized_pnl'] >= 0 else "🔴"
    ks_label = "🔴 АКТИВЕН" if snap['kill_switch'] else "🟢 Выкл"

    lines = [
        f"📈 <b>Статистика</b>\n",
        f"Режим: <b>{mode_label}</b>",
        f"Статус: <b>{_es.task_state_str()}</b>",
        f"Биржи: <b>{_es.exchange_names()}</b>",
        f"Символов: <code>{len(_es.config.symbols)}</code>",
        f"Стратегии: {', '.join(_es.config.strategy.enabled)}\n",
        f"💰 Equity: <code>${snap['equity']:.2f}</code>",
        f"{pnl_emoji} Realized PnL: <code>${snap['realized_pnl']:.2f}</code>",
        f"📊 Exposure: <code>${snap['total_exposure']:.2f}</code>",
        f"📉 Daily DD: <code>{dd['daily_dd']*100:.2f}%</code>",
        f"📉 Portfolio DD: <code>{dd['portfolio_dd']*100:.2f}%</code>",
        f"🛡 Kill switch: {ks_label}",
    ]

    if positions:
        lines.append(f"\n📂 <b>Открытые позиции ({len(positions)}):</b>")
        for pos in positions:
            age_min = (time.time() - pos.opened_at) / 60 if pos.opened_at > 0 else 0
            lines.append(
                f"  • {pos.symbol} | {pos.long_exchange}↔{pos.short_exchange} | "
                f"<code>${pos.notional_usd:.2f}</code> | {age_min:.0f}м"
            )
    else:
        lines.append("\n📂 Открытых позиций нет")

    if _es.last_error:
        lines.append(f"\n⚠️ Ошибка:\n<code>{_es.last_error[:200]}</code>")

    buttons = [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_stats")],
    ]
    if snap['kill_switch']:
        buttons.append([InlineKeyboardButton(text="🔓 Сбросить Kill Switch", callback_data="arb_reset_ks")])
    buttons.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")])

    await _safe_edit(
        call.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def cb_arb_reset_kill_switch(call: types.CallbackQuery):
    if not _es.state:
        await call.answer("Бот не запущен")
        return
    await _es.state.reset_kill_switch()
    await call.answer("Kill switch сброшен!")
    await cb_arb_stats(call)


async def cb_arb_history(call: types.CallbackQuery):
    await call.answer()
    events = list(_es.monitor.events)[-20:] if _es.monitor else []
    if not events:
        text = "📋 <b>История</b>\n\nНет событий."
    else:
        lines = ["📋 <b>История событий</b>", ""]
        for e in reversed(events):
            lines.append(f"{e['event']}: {e['payload']}")
        text = "\n".join(lines)
    await _safe_edit(call.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")]]))


async def cb_arb_pair_stats(call: types.CallbackQuery):
    await call.answer()
    await _safe_edit(call.message, "📊 Per-pair статистика пока агрегируется через события.\nИспользуйте «История».",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")]]))


async def cb_arb_funding(call: types.CallbackQuery):
    await call.answer("Загрузка...")
    if not _es.provider or not _es.config:
        await _safe_edit(call.message, "💸 Бот не запущен", reply_markup=_main_keyboard(False))
        return
    lines = ["💸 <b>Funding Rates</b>", ""]
    for symbol in _es.config.symbols[:10]:
        try:
            snapshot = await _es.provider.get_snapshot(symbol)
            if not snapshot.funding_rates:
                continue
            parts = []
            for ex, rate in sorted(snapshot.funding_rates.items()):
                rate_pct = rate * 100
                emoji = "🟢" if rate_pct > 0 else "🔴" if rate_pct < 0 else "⚪"
                parts.append(f"{emoji} {ex.upper()}: <code>{rate_pct:+.4f}%</code>")
            if len(snapshot.funding_rates) >= 2:
                vals = list(snapshot.funding_rates.values())
                diff = (max(vals) - min(vals)) * 100
                lines.append(f"<b>{symbol}</b> (diff: <code>{diff:.4f}%</code>)")
            else:
                lines.append(f"<b>{symbol}</b>")
            lines.extend(f"  {p}" for p in parts)
            lines.append("")
        except Exception:
            continue
    if len(lines) <= 2:
        lines.append("Нет данных по funding rates")
    await _safe_edit(call.message, "\n".join(lines),
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_funding")],
                         [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
                     ]))


async def cb_arb_basis(call: types.CallbackQuery):
    await call.answer("Загрузка...")
    if not _es.provider or not _es.config:
        await _safe_edit(call.message, "📉 Бот не запущен", reply_markup=_main_keyboard(False))
        return
    lines = ["📉 <b>Базис (Futures - Spot)</b>", ""]
    for symbol in _es.config.symbols[:10]:
        try:
            snapshot = await _es.provider.get_snapshot(symbol)
            basis_bps = snapshot.indicators.get("basis_bps", 0)
            spot_px = snapshot.indicators.get("spot_price", 0)
            perp_px = snapshot.indicators.get("perp_price", 0)
            if spot_px <= 0:
                continue
            emoji = "🟢" if basis_bps > 0 else "🔴" if basis_bps < 0 else "⚪"
            lines.append(
                f"{emoji} <b>{symbol}</b>: <code>{basis_bps:.2f} bps</code>\n"
                f"  Spot: <code>{spot_px:.4f}</code> | Perp: <code>{perp_px:.4f}</code>"
            )
        except Exception:
            continue
    if len(lines) <= 2:
        lines.append("Нет данных по базису")
    await _safe_edit(call.message, "\n".join(lines),
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb_basis")],
                         [InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")],
                     ]))


async def cb_arb_stat_arb(call: types.CallbackQuery):
    await call.answer()
    await _safe_edit(call.message, "📊 Stat-arb view доступен в «Возможности» (scan).",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")]]))


async def cb_arb_emergency_close(call: types.CallbackQuery):
    await call.answer("Подтвердите!")
    await _safe_edit(
        call.message,
        "🚨 <b>Экстренная остановка</b>\n\nБудет остановлен engine и запрещены новые входы.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚨 ПОДТВЕРДИТЬ", callback_data="arb_emergency_confirm")],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data="arb_menu")],
            ]
        ),
    )


async def cb_arb_emergency_confirm(call: types.CallbackQuery):
    await call.answer("Останавливаю...")
    await shutdown_arbitrage()
    await _safe_edit(call.message, "🚨 <b>Engine остановлен</b>", reply_markup=_main_keyboard(False))


async def cb_arb_settings(call: types.CallbackQuery):
    await call.answer()
    if not _es.config:
        await _safe_edit(call.message, "⚙️ Engine не запущен", reply_markup=_main_keyboard(False))
        return
    text = (
        "⚙️ <b>Настройки нового engine</b>\n\n"
        f"Dry run: {_es.config.execution.dry_run}\n"
        f"Symbols: {len(_es.config.symbols)}\n"
        f"Max symbols: {_es.config.max_symbols}\n"
        f"Strategies: {', '.join(_es.config.strategy.enabled)}\n"
        f"Max exposure: {_es.config.risk.max_total_exposure_pct:.2f}\n"
        f"Max strategy alloc: {_es.config.risk.max_strategy_allocation_pct:.2f}\n"
        f"Latency limit ms: {_es.config.risk.api_latency_limit_ms}\n"
        f"Max open positions: {_es.config.risk.max_open_positions}\n"
    )
    await _safe_edit(call.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="arb_menu")]]))


async def cb_arb_menu(call: types.CallbackQuery):
    await call.answer()
    text = f"⚡ <b>Арбитраж {_es.exchange_names()}</b>\n\n{_build_status_line()}"
    await _safe_edit(call.message, text, reply_markup=_main_keyboard(_es.running))
