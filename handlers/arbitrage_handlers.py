"""
Обработчики для арбитражного бота в Telegram
"""
import asyncio
from typing import Optional, Dict, Any, Set
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from arbitrage import ArbitrageConfig
from arbitrage.main import ArbitrageBot
from arbitrage.core import MultiPairArbitrageEngine, BotState, RiskManager, ExecutionManager, NotificationManager
from arbitrage.test.mock_exchanges import MockOKXRestClient, MockHTXRestClient
from arbitrage.strategies import StrategyManager, StrategyType
from states import ArbSettingsForm

import logging

logger = logging.getLogger(__name__)

# Глобальный экземпляр бота
_arb_bot: Optional[ArbitrageBot] = None
_arb_task: Optional[asyncio.Task] = None

# Глобальный экземпляр мульти-парного движка (старый)
_multi_pair_engine: Optional[MultiPairArbitrageEngine] = None
_multi_pair_task: Optional[asyncio.Task] = None
_multi_pair_state: Optional[BotState] = None

# Глобальный StrategyManager (новый — все 5 стратегий)
_strategy_manager: Optional[StrategyManager] = None
_strategy_task: Optional[asyncio.Task] = None

# Хранилище пользовательских настроек арбитража {user_id: {setting: value}}
_user_arb_settings: Dict[int, Dict[str, Any]] = {}

# Настройки по умолчанию
DEFAULT_ARB_SETTINGS = {
    "min_spread": 0.05,             # Минимальный спред для сделки/уведомления (%)
    "min_opportunity_lifetime": 3,  # Время стабильности перед входом (сек)
    "update_interval": 1,           # Интервал обновления цен (сек)
    "position_size": 0.01,          # Размер позиции (контракты)
    "entry_threshold": 0.25,        # Порог входа в сделку (%)
    "exit_threshold": 0.05,         # Порог выхода из сделки (%)
}


def get_user_settings(user_id: int) -> Dict[str, Any]:
    """Получить настройки пользователя (или дефолтные)"""
    if user_id not in _user_arb_settings:
        _user_arb_settings[user_id] = dict(DEFAULT_ARB_SETTINGS)
    return _user_arb_settings[user_id]


def build_settings_keyboard() -> InlineKeyboardMarkup:
    """Построить клавиатуру настроек арбитража"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Мин. спред", callback_data="arb_edit:spread")],
        [InlineKeyboardButton(text="⏱ Время стабильности", callback_data="arb_edit:lifetime")],
        [InlineKeyboardButton(text="🔄 Интервал обновления", callback_data="arb_edit:interval")],
        [InlineKeyboardButton(text="📦 Размер позиции", callback_data="arb_edit:position_size")],
        [InlineKeyboardButton(text="🎯 Порог входа (%)", callback_data="arb_edit:entry_threshold")],
        [InlineKeyboardButton(text="🚪 Порог выхода (%)", callback_data="arb_edit:exit_threshold")],
        [InlineKeyboardButton(text="🔄 Сбросить настройки", callback_data="arb_reset_settings")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="arb_back_menu")],
    ])


def build_settings_text(user_id: int, config: ArbitrageConfig) -> str:
    """Построить текст настроек"""
    s = get_user_settings(user_id)

    if config.monitoring_only:
        mode_emoji = "👀"
        mode_text = "МОНИТОРИНГ"
        mode_description = (
            "✅ OKX: read-only ключи из .env\n"
            "✅ HTX: API из .env\n"
            "ℹ️ Торговля отключена — только сканирование\n"
        )
        trade_info = ""
    elif config.dry_run_mode:
        mode_emoji = "🔒"
        mode_text = "DRY RUN (симуляция сделок)"
        mode_description = "✅ Реальные цены, сделки симулируются\n"
        trade_info = (
            f"\n<b>Параметры сделок:</b>\n"
            f"📦 Размер позиции: <b>{s.get('position_size', 0.01)} контр.</b>\n"
            f"🎯 Порог входа: <b>{s.get('entry_threshold', 0.25)}%</b>\n"
            f"🚪 Порог выхода: <b>{s.get('exit_threshold', 0.05)}%</b>\n"
            f"<i>Уведомления приходят о завершённых (симулированных) сделках с P&L</i>"
        )
    elif config.mock_mode:
        mode_emoji = "🔬"
        mode_text = "MOCK (разработка)"
        mode_description = "✅ Mock-данные, безопасное тестирование\n"
        trade_info = ""
    else:
        mode_emoji = "🔴"
        mode_text = "РЕАЛЬНАЯ ТОРГОВЛЯ"
        mode_description = "⚠️ РЕАЛЬНЫЕ деньги на реальных биржах!\n"
        trade_info = (
            f"\n<b>Параметры сделок:</b>\n"
            f"📦 Размер позиции: <b>{s.get('position_size', 0.01)} контр.</b>\n"
            f"🎯 Порог входа: <b>{s.get('entry_threshold', 0.25)}%</b>\n"
            f"🚪 Порог выхода: <b>{s.get('exit_threshold', 0.05)}%</b>\n"
            f"<i>Уведомления приходят о завершённых реальных сделках с P&L</i>"
        )

    return (
        f"⚙️ <b>Настройки арбитражного бота</b>\n\n"
        f"<b>Режим:</b> {mode_emoji} <b>{mode_text}</b>\n"
        f"{mode_description}\n"
        f"<b>Параметры сканирования:</b>\n"
        f"📈 Мин. спред: <b>{s['min_spread']}%</b>\n"
        f"⏱ Время стабильности: <b>{s['min_opportunity_lifetime']} сек</b>\n"
        f"🔄 Интервал обновления: <b>{s['update_interval']} сек</b>"
        f"{trade_info}"
    )


async def handle_arbitrage_menu(m: types.Message):
    """Показать меню арбитража"""

    # Проверяем текущий режим
    try:
        config = ArbitrageConfig.from_env()

        if config.monitoring_only:
            mode_emoji = "👀"
            mode_text = "МОНИТОРИНГ (только просмотр)"
            mode_info = (
                f"\n\n<b>Текущий режим:</b> {mode_emoji} {mode_text}\n"
                f"✅ OKX: read-only ключи из .env\n"
                f"✅ HTX: API из .env\n"
                f"✅ Торговля отключена - только мониторинг возможностей"
            )
        else:
            mode_emoji = "🔒" if config.mock_mode else "🔓"
            mode_text = "MOCK (разработка)" if config.mock_mode else "⚠️ REAL"
            mode_info = (
                f"\n\n<b>Текущий режим:</b> {mode_emoji} {mode_text}\n"
                f"{'✅ Реальные деньги НЕ тратятся' if config.mock_mode else '⚠️ ВНИМАНИЕ: Используются реальные деньги!'}"
            )
    except Exception:
        mode_info = ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Стратегии (все 5)", callback_data="arb_strategies_menu")],
        [InlineKeyboardButton(text="🔍 Быстрый мониторинг (фьючерсы)", callback_data="arb_multi_start")],
        [InlineKeyboardButton(text="⏹ Остановить мониторинг", callback_data="arb_multi_stop")],
        [InlineKeyboardButton(text="📡 Сканировать сейчас", callback_data="arb_scan_now")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="arb_settings")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]
    ])

    await m.answer(
        f"🤖 <b>Арбитражный мониторинг OKX ↔️ HTX</b>{mode_info}\n\n"
        f"Бот отслеживает арбитражные возможности и присылает уведомления.\n"
        f"Доступно <b>5 стратегий</b>: спот, фьючерсы, funding rate, basis, треугольный.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# Функционал торгового бота удален - оставлен только мониторинг
# Для активации торговли измените ARB_MONITORING_ONLY=false в .env


async def cb_arb_settings(callback: types.CallbackQuery):
    """Показать настройки бота с кнопками редактирования"""
    try:
        config = ArbitrageConfig.from_env()
        user_id = callback.from_user.id

        text = build_settings_text(user_id, config)
        keyboard = build_settings_keyboard()

        await callback.answer()
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)


async def cb_arb_edit(callback: types.CallbackQuery, state: FSMContext):
    """Начать редактирование параметра арбитража"""
    param = callback.data.split(":")[1]

    user_id = callback.from_user.id
    s = get_user_settings(user_id)

    if param == "spread":
        current = s["min_spread"]
        await state.set_state(ArbSettingsForm.entering_spread)
        prompt = (
            f"📈 <b>Минимальный спред</b>\n\n"
            f"Текущее значение: <b>{current}%</b>\n\n"
            f"Спред, при котором бот входит в сделку (или присылает уведомление в режиме мониторинга).\n"
            f"Введите число (например: <code>0.05</code>, <code>0.1</code>, <code>0.5</code>)\n"
            f"Рекомендуемый диапазон: 0.01% – 5%"
        )
    elif param == "lifetime":
        current = s["min_opportunity_lifetime"]
        await state.set_state(ArbSettingsForm.entering_lifetime)
        prompt = (
            f"⏱ <b>Время стабильности перед входом</b>\n\n"
            f"Текущее значение: <b>{current} сек</b>\n\n"
            f"Бот откроет сделку только если возможность держится заданное число секунд.\n"
            f"Введите целое число (например: <code>3</code>, <code>5</code>, <code>10</code>)\n"
            f"Рекомендуемый диапазон: 1 – 60 сек"
        )
    elif param == "interval":
        current = s["update_interval"]
        await state.set_state(ArbSettingsForm.entering_interval)
        prompt = (
            f"🔄 <b>Интервал обновления цен</b>\n\n"
            f"Текущее значение: <b>{current} сек</b>\n\n"
            f"Введите целое число (например: <code>1</code>, <code>2</code>, <code>5</code>)\n"
            f"Рекомендуемый диапазон: 1 – 30 сек"
        )
    elif param == "position_size":
        current = s.get("position_size", 0.01)
        await state.set_state(ArbSettingsForm.entering_spread)
        await state.update_data(editing_param="position_size")
        prompt = (
            f"📦 <b>Размер позиции (контракты)</b>\n\n"
            f"Текущее значение: <b>{current}</b>\n\n"
            f"Количество контрактов для каждой ноги арбитражной сделки.\n"
            f"Например: <code>0.001</code> (BTC), <code>0.01</code>, <code>0.1</code>\n"
            f"⚠️ Не превышайте допустимый риск на сделку!"
        )
    elif param == "entry_threshold":
        current = s.get("entry_threshold", 0.25)
        await state.set_state(ArbSettingsForm.entering_spread)
        await state.update_data(editing_param="entry_threshold")
        prompt = (
            f"🎯 <b>Порог входа в сделку (%)</b>\n\n"
            f"Текущее значение: <b>{current}%</b>\n\n"
            f"Минимальный спред для открытия позиции.\n"
            f"Введите число (например: <code>0.1</code>, <code>0.25</code>, <code>0.5</code>)\n"
            f"Должен быть больше порога выхода!"
        )
    elif param == "exit_threshold":
        current = s.get("exit_threshold", 0.05)
        await state.set_state(ArbSettingsForm.entering_spread)
        await state.update_data(editing_param="exit_threshold")
        prompt = (
            f"🚪 <b>Порог выхода из сделки (%)</b>\n\n"
            f"Текущее значение: <b>{current}%</b>\n\n"
            f"Бот закроет позицию когда спред опустится до этого значения.\n"
            f"Введите число (например: <code>0.02</code>, <code>0.05</code>, <code>0.1</code>)\n"
            f"Должен быть меньше порога входа!"
        )
    else:
        await callback.answer("❌ Неизвестный параметр", show_alert=True)
        return

    cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="arb_cancel_edit")]
    ])

    await callback.answer()
    await callback.message.edit_text(prompt, reply_markup=cancel_keyboard, parse_mode="HTML")


async def cb_arb_cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    """Отменить редактирование настройки"""
    await state.clear()
    try:
        config = ArbitrageConfig.from_env()
        user_id = callback.from_user.id
        text = build_settings_text(user_id, config)
        keyboard = build_settings_keyboard()
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)


async def cb_arb_reset_settings(callback: types.CallbackQuery):
    """Сбросить настройки арбитража к дефолтным"""
    user_id = callback.from_user.id
    _user_arb_settings[user_id] = dict(DEFAULT_ARB_SETTINGS)

    try:
        config = ArbitrageConfig.from_env()
        text = build_settings_text(user_id, config)
        keyboard = build_settings_keyboard()
        await callback.answer("✅ Настройки сброшены к значениям по умолчанию")
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)


async def fsm_arb_entering_spread(message: types.Message, state: FSMContext):
    """Обработчик ввода числовых параметров (спред, размер позиции, пороги)"""
    try:
        value = float(message.text.replace(",", ".").strip())
        if value <= 0:
            await message.answer("❌ Значение должно быть положительным. Попробуйте снова:")
            return

        user_id = message.from_user.id
        s = get_user_settings(user_id)

        # Определяем какой параметр редактировался
        data = await state.get_data()
        editing_param = data.get("editing_param", "min_spread")

        if editing_param == "position_size":
            if value > 10:
                await message.answer("❌ Размер позиции слишком большой (макс. 10). Попробуйте снова:")
                return
            s["position_size"] = value
            confirm_msg = f"✅ Размер позиции установлен: <b>{value} контр.</b>"
        elif editing_param == "entry_threshold":
            if value > 50:
                await message.answer("❌ Порог входа слишком большой. Попробуйте снова:")
                return
            s["entry_threshold"] = value
            confirm_msg = f"✅ Порог входа установлен: <b>{value}%</b>"
        elif editing_param == "exit_threshold":
            if value >= s.get("entry_threshold", 0.25):
                await message.answer(
                    f"❌ Порог выхода ({value}%) должен быть меньше порога входа "
                    f"({s.get('entry_threshold', 0.25)}%). Попробуйте снова:"
                )
                return
            s["exit_threshold"] = value
            confirm_msg = f"✅ Порог выхода установлен: <b>{value}%</b>"
        else:
            # min_spread по умолчанию
            if value > 50:
                await message.answer("❌ Значение должно быть от 0.01 до 50%. Попробуйте снова:")
                return
            s["min_spread"] = value
            confirm_msg = f"✅ Минимальный спред установлен: <b>{value}%</b>"

        await state.clear()
        config = ArbitrageConfig.from_env()
        text = build_settings_text(user_id, config)
        keyboard = build_settings_keyboard()
        await message.answer(
            confirm_msg + "\n\n" + text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Введите число (например: 0.25). Попробуйте снова:")


async def fsm_arb_entering_lifetime(message: types.Message, state: FSMContext):
    """Обработчик ввода времени стабильности"""
    try:
        value = int(message.text.strip())
        if value < 1 or value > 300:
            await message.answer("❌ Значение должно быть от 1 до 300 секунд. Попробуйте снова:")
            return

        user_id = message.from_user.id
        get_user_settings(user_id)["min_opportunity_lifetime"] = value
        await state.clear()

        config = ArbitrageConfig.from_env()
        text = build_settings_text(user_id, config)
        keyboard = build_settings_keyboard()
        await message.answer(
            f"✅ Время стабильности установлено: <b>{value} сек</b>\n\n" + text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Введите целое число (например: 5). Попробуйте снова:")


async def fsm_arb_entering_interval(message: types.Message, state: FSMContext):
    """Обработчик ввода интервала обновления"""
    try:
        value = int(message.text.strip())
        if value < 1 or value > 60:
            await message.answer("❌ Значение должно быть от 1 до 60 секунд. Попробуйте снова:")
            return

        user_id = message.from_user.id
        get_user_settings(user_id)["update_interval"] = value
        await state.clear()

        config = ArbitrageConfig.from_env()
        text = build_settings_text(user_id, config)
        keyboard = build_settings_keyboard()
        await message.answer(
            f"✅ Интервал обновления установлен: <b>{value} сек</b>\n\n" + text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Введите целое число (например: 1). Попробуйте снова:")


async def cb_arb_back_menu(callback: types.CallbackQuery):
    """Вернуться в меню арбитража"""
    try:
        config = ArbitrageConfig.from_env()

        if config.monitoring_only:
            mode_emoji = "👀"
            mode_text = "МОНИТОРИНГ (только просмотр)"
            mode_info = (
                f"\n\n<b>Текущий режим:</b> {mode_emoji} {mode_text}\n"
                f"✅ OKX: read-only ключи из .env\n"
                f"✅ HTX: API из .env\n"
                f"✅ Торговля отключена - только мониторинг возможностей"
            )
        else:
            mode_emoji = "🔒" if config.mock_mode else "🔓"
            mode_text = "MOCK (разработка)" if config.mock_mode else "⚠️ REAL"
            mode_info = (
                f"\n\n<b>Текущий режим:</b> {mode_emoji} {mode_text}\n"
                f"{'✅ Реальные деньги НЕ тратятся' if config.mock_mode else '⚠️ ВНИМАНИЕ: Используются реальные деньги!'}"
            )
    except Exception:
        mode_info = ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Стратегии (все 5)", callback_data="arb_strategies_menu")],
        [InlineKeyboardButton(text="🔍 Быстрый мониторинг (фьючерсы)", callback_data="arb_multi_start")],
        [InlineKeyboardButton(text="⏹ Остановить мониторинг", callback_data="arb_multi_stop")],
        [InlineKeyboardButton(text="📡 Сканировать сейчас", callback_data="arb_scan_now")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="arb_settings")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]
    ])

    await callback.answer()
    await callback.message.edit_text(
        f"🤖 <b>Арбитражный мониторинг OKX ↔️ HTX</b>{mode_info}\n\n"
        f"Бот отслеживает арбитражные возможности и присылает уведомления.\n"
        f"Доступно <b>5 стратегий</b>: спот, фьючерсы, funding rate, basis, треугольный.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


async def cb_back_main(callback: types.CallbackQuery):
    """Вернуться в главное меню"""
    from keyboards import main_menu
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu())


# ══════════════════════════════════════════════════════════════════════════════
#  Меню стратегий
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_INFO = {
    StrategyType.SPOT_ARB: {
        "emoji": "🔄",
        "name": "Спот-арбитраж",
        "desc": "Покупка на дешёвой бирже, продажа на дорогой.\nTреб. баланс на обеих биржах.",
        "min_profit": "~0.1% после комиссий",
    },
    StrategyType.FUTURES_ARB: {
        "emoji": "📊",
        "name": "Фьючерсный арбитраж",
        "desc": "LONG на одной бирже, SHORT на другой по перп-фьючерсам.\nПрибыль при схождении цен.",
        "min_profit": "~0.05% спред",
    },
    StrategyType.FUNDING_ARB: {
        "emoji": "💸",
        "name": "Funding Rate арбитраж",
        "desc": "SHORT там где funding+, LONG там где funding−.\nДоход каждые 8 часов.",
        "min_profit": "~0.02% за 8ч ≈ до 27% год.",
    },
    StrategyType.BASIS_ARB: {
        "emoji": "⚖️",
        "name": "Basis арбитраж",
        "desc": "Игра на разнице фьючерс vs спот.\nКупить спот + SHORT перп (Cash & Carry).",
        "min_profit": "~0.15% базис",
    },
    StrategyType.TRIANGULAR: {
        "emoji": "🔺",
        "name": "Треугольный арбитраж",
        "desc": "USDT→BTC→ETH→USDT внутри одной биржи.\nТребует сверхнизкой задержки.",
        "min_profit": "~0.05% после 3 комиссий",
    },
}


def _build_strategies_keyboard(enabled: Set[StrategyType], is_running: bool) -> InlineKeyboardMarkup:
    """Клавиатура выбора стратегий"""
    rows = []
    for st in StrategyType:
        info = STRATEGY_INFO[st]
        active = "✅" if st in enabled else "⬜"
        rows.append([InlineKeyboardButton(
            text=f"{active} {info['emoji']} {info['name']}",
            callback_data=f"arb_toggle_strategy:{st.value}"
        )])

    action_text = "⏹ Остановить все" if is_running else "▶️ Запустить выбранные"
    action_cb = "arb_strategies_stop" if is_running else "arb_strategies_start"

    rows.append([InlineKeyboardButton(text=action_text, callback_data=action_cb)])
    rows.append([InlineKeyboardButton(text="📡 Сканировать все стратегии", callback_data="arb_strategies_scan")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="arb_back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_user_enabled_strategies(user_id: int) -> Set[StrategyType]:
    """Получить включённые стратегии пользователя"""
    s = get_user_settings(user_id)
    raw = s.get("enabled_strategies", {StrategyType.FUTURES_ARB, StrategyType.FUNDING_ARB, StrategyType.BASIS_ARB})
    return raw


def _set_user_enabled_strategies(user_id: int, strategies: Set[StrategyType]) -> None:
    get_user_settings(user_id)["enabled_strategies"] = strategies


async def cb_arb_strategies_menu(callback: types.CallbackQuery):
    """Показать меню выбора стратегий"""
    user_id = callback.from_user.id
    enabled = _get_user_enabled_strategies(user_id)
    is_running = (
        _strategy_manager is not None
        and _strategy_task is not None
        and not _strategy_task.done()
    )

    lines = ["🎯 <b>Арбитражные стратегии</b>\n"]
    for st in StrategyType:
        info = STRATEGY_INFO[st]
        status = "✅ вкл" if st in enabled else "⬜ выкл"
        lines.append(
            f"{info['emoji']} <b>{info['name']}</b> [{status}]\n"
            f"   {info['desc']}\n"
            f"   💰 Порог: {info['min_profit']}\n"
        )

    if is_running:
        # Показываем открытые сделки если есть
        status = _strategy_manager.get_status() if _strategy_manager else {}
        open_trades = status.get("open_trades", {})
        total_open = sum(open_trades.values())
        mode = status.get("mode", "monitoring_only")

        mode_label = {
            "monitoring_only": "👀 мониторинг",
            "dry_run": "🔒 dry run",
            "real": "🔴 реальная торговля",
        }.get(mode, mode)

        lines.append(f"\n🟢 <b>Бот запущен</b> [{mode_label}]")
        if total_open > 0:
            lines.append(f"📂 Открытых сделок: <b>{total_open}</b>")
    else:
        lines.append("\n⚫ Бот остановлен")

    await callback.answer()
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_build_strategies_keyboard(enabled, is_running),
        parse_mode="HTML"
    )


async def cb_arb_toggle_strategy(callback: types.CallbackQuery):
    """Включить/выключить стратегию"""
    strategy_value = callback.data.split(":")[1]
    try:
        strategy = StrategyType(strategy_value)
    except ValueError:
        await callback.answer("Неизвестная стратегия", show_alert=True)
        return

    user_id = callback.from_user.id
    enabled = _get_user_enabled_strategies(user_id)

    if strategy in enabled:
        enabled.discard(strategy)
        await callback.answer(f"⬜ {STRATEGY_INFO[strategy]['name']} отключена")
    else:
        enabled.add(strategy)
        await callback.answer(f"✅ {STRATEGY_INFO[strategy]['name']} включена")

    _set_user_enabled_strategies(user_id, enabled)

    is_running = (
        _strategy_manager is not None
        and _strategy_task is not None
        and not _strategy_task.done()
    )
    await callback.message.edit_reply_markup(
        reply_markup=_build_strategies_keyboard(enabled, is_running)
    )


async def cb_arb_strategies_start(callback: types.CallbackQuery):
    """Запустить StrategyManager со всеми выбранными стратегиями"""
    global _strategy_manager, _strategy_task

    if _strategy_task and not _strategy_task.done():
        await callback.answer("⚠️ Мониторинг уже запущен", show_alert=True)
        return

    user_id = callback.from_user.id
    enabled = _get_user_enabled_strategies(user_id)

    if not enabled:
        await callback.answer("⚠️ Выберите хотя бы одну стратегию", show_alert=True)
        return

    try:
        config = ArbitrageConfig.from_env()
        user_s = get_user_settings(user_id)

        # Применяем все пользовательские настройки
        config.min_spread = user_s["min_spread"]
        config.min_opportunity_lifetime = user_s["min_opportunity_lifetime"]
        config.update_interval = user_s["update_interval"]
        config.position_size = user_s.get("position_size", config.position_size)
        config.entry_threshold = user_s.get("entry_threshold", config.entry_threshold)
        config.exit_threshold = user_s.get("exit_threshold", config.exit_threshold)

        from arbitrage.exchanges import OKXRestClient, HTXRestClient
        from arbitrage.utils import ExchangeConfig

        if config.monitoring_only:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())
        elif config.mock_mode:
            okx_client = MockOKXRestClient(config.get_okx_config())
            htx_client = MockHTXRestClient(config.get_htx_config())
        else:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())

        notification_manager = NotificationManager(callback.bot, user_id)

        _strategy_manager = StrategyManager(
            config=config,
            okx_client=okx_client,
            htx_client=htx_client,
            notification_manager=notification_manager,
            enabled_strategies=enabled,
        )

        await _strategy_manager.initialize()
        _strategy_task = asyncio.create_task(_strategy_manager.start())

        strategy_names = "\n".join(
            f"  {STRATEGY_INFO[s]['emoji']} {STRATEGY_INFO[s]['name']}"
            for s in enabled
        )

        # Режим-зависимое сообщение
        if config.monitoring_only:
            mode_msg = "👀 Режим: только мониторинг. Сделки не открываются."
        elif config.dry_run_mode:
            mode_msg = (
                f"🔒 Режим: DRY RUN (симуляция).\n"
                f"Сделки симулируются. Уведомления приходят о завершённых сделках с P&L.\n"
                f"Размер позиции: {config.position_size} контр. | "
                f"Вход: {config.entry_threshold}% | Выход: {config.exit_threshold}%"
            )
        else:
            mode_msg = (
                f"🔴 Режим: РЕАЛЬНАЯ ТОРГОВЛЯ!\n"
                f"Реальные сделки. Уведомления приходят о завершённых сделках с P&L.\n"
                f"Размер позиции: {config.position_size} контр. | "
                f"Вход: {config.entry_threshold}% | Выход: {config.exit_threshold}%"
            )

        await callback.answer("✅ Бот запущен", show_alert=True)
        await callback.message.edit_text(
            f"🟢 <b>Бот запущен</b>\n\n"
            f"<b>Активные стратегии:</b>\n{strategy_names}\n\n"
            f"{mode_msg}",
            reply_markup=_build_strategies_keyboard(enabled, True),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Strategy manager start error: {e}", exc_info=True)
        await callback.answer(f"❌ Ошибка: {str(e)[:100]}", show_alert=True)


async def cb_arb_strategies_stop(callback: types.CallbackQuery):
    """Остановить StrategyManager"""
    global _strategy_manager, _strategy_task

    if not _strategy_manager or not _strategy_task or _strategy_task.done():
        await callback.answer("⚠️ Мониторинг не запущен", show_alert=True)
        return

    try:
        _strategy_manager.stop()
        if not _strategy_task.done():
            _strategy_task.cancel()

        user_id = callback.from_user.id
        enabled = _get_user_enabled_strategies(user_id)

        await callback.answer("✅ Бот остановлен")
        await callback.message.edit_text(
            "⚫ <b>Бот остановлен</b>\n\nВсе открытые сделки закрыты или будут закрыты при старте.\nМожно запустить снова в любое время.",
            reply_markup=_build_strategies_keyboard(enabled, False),
            parse_mode="HTML"
        )

        _strategy_manager = None
        _strategy_task = None

    except Exception as e:
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)


async def cb_arb_strategies_scan(callback: types.CallbackQuery):
    """Однократный скан всех стратегий"""
    await callback.answer("⏳ Сканирую все стратегии...", show_alert=False)

    user_id = callback.from_user.id
    enabled = _get_user_enabled_strategies(user_id)

    try:
        config = ArbitrageConfig.from_env()

        from arbitrage.exchanges import OKXRestClient, HTXRestClient
        from arbitrage.utils import ExchangeConfig

        if config.monitoring_only:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())
        elif config.mock_mode:
            okx_client = MockOKXRestClient(config.get_okx_config())
            htx_client = MockHTXRestClient(config.get_htx_config())
        else:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())

        manager = StrategyManager(
            config=config,
            okx_client=okx_client,
            htx_client=htx_client,
            notification_manager=NotificationManager(),
            enabled_strategies=enabled,
        )
        await manager.initialize()
        scan_results = await manager.scan_all()

        # Закрываем сессии
        for client in [okx_client, htx_client]:
            if hasattr(client, 'session') and client.session:
                await client.session.close()

        # Форматируем результаты
        lines = ["📡 <b>Скан всех стратегий</b>\n"]
        user_s = get_user_settings(user_id)
        min_spread = user_s["min_spread"]

        SECTION_NAMES = {
            "spot": "🔄 Спот-арбитраж (чист. прибыль, %)",
            "futures": "📊 Фьючерсный арбитраж (спред, %)",
            "funding": "💸 Funding Rate (разница ставок, %/8ч)",
            "basis": "⚖️ Basis (фьючерс vs спот, %)",
            "triangular": "🔺 Треугольный (чист. прибыль, %)",
        }

        any_data = False
        for key, title in SECTION_NAMES.items():
            items = scan_results.get(key, [])
            if not items:
                continue
            any_data = True
            lines.append(f"\n<b>{title}</b>")

            for item in items[:3]:
                if key == "spot":
                    val = item.get("net_profit_pct", 0)
                    mark = "🟢" if val >= min_spread else "⚪"
                    lines.append(f"  {mark} {item['symbol']}: {val:.3f}%")
                elif key == "futures":
                    val = item.get("spread_pct", 0)
                    mark = "🟢" if val >= min_spread else "⚪"
                    lines.append(f"  {mark} {item['symbol']}: {val:.3f}%")
                elif key == "funding":
                    val = item.get("diff_pct", 0)
                    mark = "🟢" if val >= 0.02 else "⚪"
                    lines.append(
                        f"  {mark} {item['symbol']}: diff={val:.4f}% "
                        f"(OKX {item['okx_rate']:+.4f}% / HTX {item.get('htx_rate', item.get('bybit_rate', 0)):+.4f}%)"
                    )
                elif key == "basis":
                    val = item.get("basis_pct", 0)
                    mark = "🟢" if abs(val) >= 0.15 else "⚪"
                    lines.append(
                        f"  {mark} {item['symbol']}: {val:+.3f}% "
                        f"({item['spot_exchange'].upper()} spot)"
                    )
                elif key == "triangular":
                    val = item.get("profit_pct", 0)
                    mark = "🟢" if val >= 0.05 else "⚪"
                    lines.append(
                        f"  {mark} {item['name']} ({item['exchange'].upper()}): {val:.3f}%"
                    )

        if not any_data:
            lines.append("\n⚠️ Не удалось получить данные.\nПроверьте API ключи.")

        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К стратегиям", callback_data="arb_strategies_menu")]
        ])
        await callback.message.edit_text(
            "\n".join(lines), reply_markup=back_kb, parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Strategy scan error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка сканирования: {str(e)[:150]}")


async def cb_arb_scan_now(callback: types.CallbackQuery):
    """Разовый скан топ спредов без фильтра порогов"""
    await callback.answer("⏳ Сканирую биржи...", show_alert=False)

    # Если мониторинг уже запущен — используем его данные
    if _multi_pair_engine and _multi_pair_task and not _multi_pair_task.done():
        try:
            # Обновляем цены и получаем спреды без фильтра min_spread
            await _multi_pair_engine.update_prices()

            # Временно убираем порог для получения всех спредов
            orig_min_spread = _multi_pair_engine.min_spread
            _multi_pair_engine.min_spread = 0.0
            all_spreads = await _multi_pair_engine.calculate_spreads()
            _multi_pair_engine.min_spread = orig_min_spread

            await _send_scan_result(callback, all_spreads, _multi_pair_engine.config)
            return
        except Exception as e:
            logger.error(f"Scan using running engine failed: {e}", exc_info=True)

    # Мониторинг не запущен — создаём временные клиенты
    try:
        config = ArbitrageConfig.from_env()

        from arbitrage.exchanges import OKXRestClient, HTXRestClient
        from arbitrage.utils import ExchangeConfig
        from arbitrage.core import BotState, RiskManager, ExecutionManager

        if config.monitoring_only:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())
        elif config.mock_mode:
            okx_client = MockOKXRestClient(config.get_okx_config())
            htx_client = MockHTXRestClient(config.get_htx_config())
        else:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())

        state = BotState()
        engine = MultiPairArbitrageEngine(
            config, state,
            RiskManager(config, state),
            ExecutionManager(config, state, okx_client, htx_client),
            okx_client, htx_client
        )

        # Инициализация (получение списка пар)
        await engine.initialize()

        # Обновляем цены
        await engine.update_prices()

        # Получаем спреды без порога
        engine.min_spread = 0.0
        all_spreads = await engine.calculate_spreads()

        await _send_scan_result(callback, all_spreads, config)

        # Закрываем сессии
        if hasattr(okx_client, 'session') and okx_client.session:
            await okx_client.session.close()
        if hasattr(htx_client, 'session') and htx_client.session:
            await htx_client.session.close()

    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка сканирования: {str(e)}")


async def _send_scan_result(callback: types.CallbackQuery, all_spreads, config):
    """Отправить результаты сканирования"""
    user_id = callback.from_user.id
    user_s = get_user_settings(user_id)
    user_min_spread = user_s["min_spread"]

    if not all_spreads:
        text = (
            f"📡 <b>Результаты сканирования</b>\n\n"
            f"⚠️ Не удалось получить данные с бирж.\n"
            f"Проверьте API ключи и соединение."
        )
    else:
        top = all_spreads[:10]
        lines = []
        for i, s in enumerate(top, 1):
            marker = "🟢" if s.spread >= user_min_spread else "⚪"
            lines.append(
                f"{marker} {i}. <b>{s.symbol}</b>: {s.spread:.3f}%\n"
                f"   LONG {s.get_long_exchange().upper()} @ {s.get_long_price():,.4f}\n"
                f"   SHORT {s.get_short_exchange().upper()} @ {s.get_short_price():,.4f}"
            )

        above = sum(1 for s in all_spreads if s.spread >= user_min_spread)
        text = (
            f"📡 <b>Топ спреды прямо сейчас</b>\n\n"
            + "\n\n".join(lines)
            + f"\n\n🟢 Выше порога ({user_min_spread}%): <b>{above}</b> пар\n"
            f"⚪ Всего с положительным спредом: {len(all_spreads)}\n\n"
            f"<i>Уведомления приходят только о 🟢 парах, "
            f"державшихся {config.min_opportunity_lifetime}+ сек подряд</i>"
        )

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="arb_back_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")


async def cb_arb_multi_start(callback: types.CallbackQuery):
    """Запустить мониторинг всех пар"""
    global _multi_pair_engine, _multi_pair_task, _multi_pair_state

    if _multi_pair_engine and _multi_pair_task and not _multi_pair_task.done():
        await callback.answer("⚠️ Мониторинг уже запущен", show_alert=True)
        return

    try:
        # Загрузка конфигурации
        config = ArbitrageConfig.from_env()

        # Применяем пользовательские настройки поверх конфига
        user_id = callback.from_user.id
        user_s = get_user_settings(user_id)
        config.min_spread = user_s["min_spread"]
        config.min_opportunity_lifetime = user_s["min_opportunity_lifetime"]
        config.update_interval = user_s["update_interval"]

        # Создание компонентов
        _multi_pair_state = BotState()
        _multi_pair_state.is_running = True

        # Создание клиентов
        from arbitrage.exchanges import OKXRestClient, HTXRestClient
        from arbitrage.utils import ExchangeConfig

        if config.monitoring_only:
            # В режиме monitoring_only:
            # - OKX с ключами из .env
            # - HTX с ключами из .env (или без для публичных endpoint'ов)
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())
            logger.info("Monitoring-only mode: OKX with keys from .env, HTX API")
        elif config.mock_mode:
            okx_client = MockOKXRestClient(config.get_okx_config())
            htx_client = MockHTXRestClient(config.get_htx_config())
        else:
            okx_client = OKXRestClient(config.get_okx_config())
            htx_client = HTXRestClient(config.get_htx_config())

        # Создание менеджеров
        risk_manager = RiskManager(config, _multi_pair_state)
        execution_manager = ExecutionManager(
            config, _multi_pair_state, okx_client, htx_client
        )
        notification_manager = NotificationManager(callback.bot, callback.from_user.id)

        # Создание движка мульти-пар
        _multi_pair_engine = MultiPairArbitrageEngine(
            config,
            _multi_pair_state,
            risk_manager,
            execution_manager,
            okx_client,
            htx_client,
            notification_manager
        )

        # Инициализация
        await _multi_pair_engine.initialize()

        # Запуск мониторинга в фоне
        _multi_pair_task = asyncio.create_task(_multi_pair_engine.start_monitoring())

        await callback.answer("✅ Мониторинг всех пар запущен", show_alert=True)

        if config.monitoring_only:
            mode_emoji = "👀"
            mode_text = "МОНИТОРИНГ (только просмотр)"
        else:
            mode_emoji = "🔒" if config.mock_mode else "🔓"
            mode_text = "MOCK (разработка)" if config.mock_mode else "⚠️ REAL"

        await callback.message.edit_text(
            f"🔍 <b>Мониторинг запущен</b>\n\n"
            f"Режим: {mode_emoji} <b>{mode_text}</b>\n\n"
            f"<b>Что отслеживается:</b>\n"
            f"📊 {len(_multi_pair_engine.monitored_pairs)} торговых пар\n"
            f"💱 Примеры: {', '.join(sorted(list(_multi_pair_engine.monitored_pairs)[:5]))}...\n\n"
            f"<b>Параметры:</b>\n"
            f"📈 Минимальный спред: <b>{_multi_pair_engine.min_spread}%</b>\n"
            f"⏱ Время стабильности: <b>{config.min_opportunity_lifetime} сек</b>\n"
            f"🔄 Обновление: каждые <b>{_multi_pair_engine.update_interval} сек</b>\n\n"
            f"{'✅ OKX (read-only) + HTX API' if config.monitoring_only else '💰 Уведомления о возможностях'}\n\n"
            f"<i>Уведомления приходят только о стабильных возможностях</i>",
            parse_mode="HTML"
        )

    except Exception as e:
        await callback.answer(f"❌ Ошибка запуска: {str(e)}", show_alert=True)
        logger.error(f"Multi-pair start error: {e}", exc_info=True)


async def cb_arb_multi_stop(callback: types.CallbackQuery):
    """Остановить мониторинг всех пар"""
    global _multi_pair_engine, _multi_pair_task, _multi_pair_state

    if not _multi_pair_engine or not _multi_pair_task or _multi_pair_task.done():
        await callback.answer("⚠️ Мониторинг не запущен", show_alert=True)
        return

    try:
        # Остановка
        _multi_pair_state.is_running = False

        if not _multi_pair_task.done():
            _multi_pair_task.cancel()

        await callback.answer("✅ Мониторинг остановлен", show_alert=True)
        await callback.message.edit_text(
            "🔍 <b>Мониторинг всех пар остановлен</b>\n\n"
            "Можете запустить снова в любое время",
            parse_mode="HTML"
        )

        _multi_pair_engine = None
        _multi_pair_task = None
        _multi_pair_state = None

    except Exception as e:
        await callback.answer(f"❌ Ошибка остановки: {str(e)}", show_alert=True)
        logger.error(f"Multi-pair stop error: {e}", exc_info=True)
