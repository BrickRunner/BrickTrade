import io
from datetime import date, timedelta
import matplotlib.pyplot as plt
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types.input_file import BufferedInputFile
import aiohttp

from database import get_settings
from api import fetch_historical_data
from keyboards import build_stats_currencies_kb, build_stats_period_kb, main_menu


async def handle_stats(m: types.Message):
    """Обработка запроса статистики"""
    row = await get_settings(m.from_user.id)
    if not row or not row[1]:
        await m.answer("❌ У вас не выбраны валюты в настройках.", reply_markup=main_menu())
        return
    
    currencies = [c.strip() for c in row[1].split(",") if c.strip()]
    if not currencies:
        await m.answer("❌ Нет доступных валют для статистики.", reply_markup=main_menu())
        return
    
    kb = build_stats_currencies_kb(currencies)
    await m.answer("📊 Выберите валюту для статистики:", reply_markup=kb)


async def cb_stats(cb: types.CallbackQuery):
    """Обработка callback для статистики"""
    await handle_stats_for_callback(cb)


async def handle_stats_for_callback(cb: types.CallbackQuery):
    """Обработка возврата в меню статистики"""
    row = await get_settings(cb.from_user.id)
    if not row or not row[1]:
        await cb.message.answer("❌ У вас не выбраны валюты в настройках.", reply_markup=main_menu())
        try:
            await cb.answer()
        except TelegramBadRequest:
            pass
        return
    
    currencies = [c.strip() for c in row[1].split(",") if c.strip()]
    if not currencies:
        await cb.message.answer("❌ Нет доступных валют для статистики.", reply_markup=main_menu())
        try:
            await cb.answer()
        except TelegramBadRequest:
            pass
        return
    
    kb = build_stats_currencies_kb(currencies)
    try:
        await cb.message.edit_text("📊 Выберите валюту для статистики:", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer("📊 Выберите валюту для статистики:", reply_markup=kb)
    try:
        await cb.answer()
    except TelegramBadRequest:
        pass


async def cb_stats_period(cb: types.CallbackQuery):
    """Обработка выбора валюты для статистики"""
    try:
        currency = cb.data.split(":", 1)[1].strip()
    except (IndexError, AttributeError):
        await cb.answer("Ошибка данных")
        return
    kb = build_stats_period_kb(currency)
    try:
        await cb.message.edit_text(f"Выберите период для {currency}:", reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(f"Выберите период для {currency}:", reply_markup=kb)
    await cb.answer()


async def cb_show_graph(cb: types.CallbackQuery):
    """Обработка построения графика статистики"""
    parts = cb.data.split(":")
    if len(parts) != 3:
        try:
            await cb.answer("❌ Ошибка в данных периода.", show_alert=True)
        except TelegramBadRequest:
            await cb.message.answer("❌ Ошибка в данных периода.")
        return
    
    _, currency, days_str = parts
    try:
        days = int(days_str)
    except ValueError:
        try:
            await cb.answer("❌ Некорректный период.", show_alert=True)
        except TelegramBadRequest:
            await cb.message.answer("❌ Некорректный период.")
        return
    
    if days not in [7, 30, 365]:
        try:
            await cb.answer("❌ Поддерживаются только 7, 30 или 365 дней.", show_alert=True)
        except TelegramBadRequest:
            await cb.message.answer("❌ Поддерживаются только 7, 30 или 365 дней.")
        return
    
    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)
    
    try:
        await cb.answer("⏳ Получаю данные...")
    except TelegramBadRequest:
        await cb.message.answer("⏳ Получаю данные...")
    
    await cb.message.answer("📈 График строится, ожидайте...")
    
    try:
        data = await fetch_historical_data(currency, start_date, end_date)
        
        if not data:
            await cb.message.answer(
                f"❌ Данных за выбранный период для {currency} нет (возможно, выходные/праздники).",
                reply_markup=main_menu()
            )
            return
        
        data.sort(key=lambda x: x[0])
        dates, values = zip(*data)
        
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(dates, values, marker="o", linewidth=2, markersize=4)
        ax.set_title(
            f"Курс {currency} к RUB за {days} дней\n"
            f"(Данные ЦБ РФ, {start_date.strftime('%d.%m.%Y')} — {end_date.strftime('%d.%m.%Y')})",
            fontsize=12
        )
        ax.set_xlabel("Дата")
        ax.set_ylabel("RUB")
        ax.grid(True, linestyle="--", alpha=0.6)
        ax.tick_params(axis='x', rotation=45)
        plt.tight_layout(pad=1.0)
        
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, bbox_inches='tight')
        buf.seek(0)
        photo = BufferedInputFile(buf.getvalue(), filename="graph.png")
        plt.close(fig)
        
        caption = f"📊 Динамика курса {currency} за {days} дней.\nТочки — ежедневные значения."
        await cb.message.answer_photo(photo=photo, caption=caption, reply_markup=main_menu())
        await cb.message.answer("✅ График отправлен!")
        
    except ValueError as e:
        await cb.message.answer(f"⚠️ {str(e)}", reply_markup=main_menu())
    except aiohttp.ClientError as e:
        await cb.message.answer(f"❌ Ошибка сети при загрузке данных: {str(e)}", reply_markup=main_menu())
    except Exception as e:
        await cb.message.answer(f"❌ Неожиданная ошибка в статистике: {str(e)}", reply_markup=main_menu())
    finally:
        try:
            await handle_stats_for_callback(cb)
        except TelegramBadRequest:
            pass