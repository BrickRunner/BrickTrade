from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from database import get_settings
from api import fetch_all_rates
from datetime import datetime, timedelta
import logging
from typing import List

logger = logging.getLogger(__name__)

# Кеш для списка валют
_currency_cache = None
_cache_timestamp = None
CACHE_TTL_SECONDS = 3600  # Кеш на 1 час

# Константы для числа кнопок в ряду
KEYBOARD_COLUMNS_CURRENCIES = 3
KEYBOARD_COLUMNS_DAYS = 3
KEYBOARD_COLUMNS_TIMEZONE = 4


async def get_all_currencies() -> List[str]:
    """Получение списка всех валют с кешированием"""
    global _currency_cache, _cache_timestamp

    now = datetime.utcnow()

    # Проверка кеша
    if _currency_cache is not None and _cache_timestamp is not None:
        if (now - _cache_timestamp).total_seconds() < CACHE_TTL_SECONDS:
            logger.debug("Using cached currency list")
            return _currency_cache

    # Обновление кеша
    try:
        logger.info("Fetching fresh currency list")
        all_data = await fetch_all_rates()
        _currency_cache = sorted(all_data["rates"].keys())
        _cache_timestamp = now
        return _currency_cache
    except Exception as e:
        logger.error(f"Failed to fetch currency list: {e}", exc_info=True)
        # Если кеш устарел, но запрос не удался, используем старый кеш
        if _currency_cache is not None:
            logger.warning("Using stale cache due to fetch error")
            return _currency_cache
        raise


def main_menu() -> ReplyKeyboardMarkup:
    """Создание главного меню"""
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Курсы валют сейчас")],
            [KeyboardButton(text="📉 Пороговые значения")],
            [KeyboardButton(text="📈 Статистика")],
            [KeyboardButton(text="🤖 Арбитраж OKX/Bybit")],
            [KeyboardButton(text="⚙ Настройки")]
        ],
        resize_keyboard=True
    )
    return kb


def settings_menu() -> InlineKeyboardMarkup:
    """Создание меню настроек"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💱 Валюты", callback_data="set_currencies")],
        [InlineKeyboardButton(text="⏰ Время", callback_data="set_time")],
        [InlineKeyboardButton(text="📅 Дни", callback_data="set_days")],
        [InlineKeyboardButton(text="🌍 Часовой пояс", callback_data="set_timezone")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="back_settings")]
    ])


def thresholds_menu() -> InlineKeyboardMarkup:
    """Создание меню пороговых значений"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить порог", callback_data="add_threshold"),
         InlineKeyboardButton(text="➖ Удалить порог", callback_data="del_thresholds")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="back_main")]
    ])


async def build_currencies_kb(selected: List[str]) -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора валют с кешированием"""
    kb = []
    row = []
    cnt = 0

    try:
        all_codes = await get_all_currencies()
    except Exception as e:
        logger.error(f"Failed to build currencies keyboard: {e}")
        # Минимальная клавиатура при ошибке
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Назад", callback_data="back_settings")]
        ])

    for c in all_codes:
        mark = "✅" if c in selected else "❌"
        row.append(InlineKeyboardButton(text=f"{mark} {c}", callback_data=f"toggle_curr:{c}"))
        cnt += 1
        if cnt % KEYBOARD_COLUMNS_CURRENCIES == 0:
            kb.append(row)
            row = []

    if row:
        kb.append(row)

    kb.append([InlineKeyboardButton(text="⬅ Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def build_threshold_currency_kb(user_id: int) -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора валюты порога"""
    try:
        row = await get_settings(user_id)
        selected_currencies = [c.strip().upper() for c in (row[1] or "USD,EUR").split(",") if c.strip()]
        kb = []
        row_buttons = []

        for idx, c in enumerate(selected_currencies, 1):
            row_buttons.append(InlineKeyboardButton(text=c, callback_data=f"th_curr:{c}"))
            if idx % KEYBOARD_COLUMNS_CURRENCIES == 0:
                kb.append(row_buttons)
                row_buttons = []

        if row_buttons:
            kb.append(row_buttons)

        kb.append([InlineKeyboardButton(text="⬅ Назад", callback_data="back_main")])
        return InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception as e:
        logger.error(f"Failed to build threshold currency keyboard for user {user_id}: {e}")
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Назад", callback_data="back_main")]
        ])


def build_days_kb(selected: List[str]) -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора дней"""
    days_map = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
    kb = []
    row_buttons = []
    cnt = 0

    for i in range(1, 8):
        mark = "✅" if str(i) in selected else "❌"
        row_buttons.append(InlineKeyboardButton(text=f"{mark} {days_map[i]}", callback_data=f"toggle_day:{i}"))
        cnt += 1
        if cnt % KEYBOARD_COLUMNS_DAYS == 0:
            kb.append(row_buttons)
            row_buttons = []

    if row_buttons:
        kb.append(row_buttons)

    kb.append([InlineKeyboardButton(text="⬅ Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def build_timezone_kb() -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора часового пояса (UTC)"""
    kb_rows = []
    row = []
    cnt = 0

    for tz in range(-12, 13):
        # Форматирование: UTC+3, UTC-5, UTC+0
        if tz == 0:
            text = "UTC+0"
        else:
            text = f"UTC{tz:+d}"
        row.append(InlineKeyboardButton(text=text, callback_data=f"set_tz:{tz}"))
        cnt += 1
        if cnt % KEYBOARD_COLUMNS_TIMEZONE == 0:
            kb_rows.append(row)
            row = []

    if row:
        kb_rows.append(row)

    kb_rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def build_stats_currencies_kb(currencies: List[str]) -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора валюты статистики"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=c, callback_data=f"stats_curr:{c}")] for c in currencies
    ] + [[InlineKeyboardButton(text="⬅ Назад", callback_data="back_main")]])


def build_stats_period_kb(currency: str) -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора периода статистики"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя", callback_data=f"stats_period:{currency}:7")],
        [InlineKeyboardButton(text="🗓 Месяц", callback_data=f"stats_period:{currency}:30")],
        [InlineKeyboardButton(text="📆 Год", callback_data=f"stats_period:{currency}:365")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="stats")]
    ])