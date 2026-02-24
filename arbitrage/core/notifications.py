"""
Система уведомлений для арбитражного бота
"""
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("notifications")


class NotificationManager:
    """Менеджер уведомлений в Telegram"""

    def __init__(self, bot=None, user_id: Optional[int] = None):
        """
        Args:
            bot: Экземпляр Telegram бота (aiogram Bot)
            user_id: ID пользователя для отправки уведомлений
        """
        self.bot = bot
        self.user_id = user_id
        self.enabled = True
        self.notification_queue = asyncio.Queue()

    def set_bot(self, bot, user_id: int):
        """Установить бота и пользователя"""
        self.bot = bot
        self.user_id = user_id
        logger.info(f"Notification bot set for user {user_id}")

    def enable(self):
        """Включить уведомления"""
        self.enabled = True
        logger.info("Notifications enabled")

    def disable(self):
        """Выключить уведомления"""
        self.enabled = False
        logger.info("Notifications disabled")

    async def send(self, message: str, parse_mode: str = "HTML"):
        """
        Отправить уведомление

        Args:
            message: Текст сообщения
            parse_mode: Режим форматирования (HTML/Markdown)
        """
        if not self.enabled:
            logger.debug("Notifications disabled, skipping")
            return

        if not self.bot or not self.user_id:
            logger.warning("Bot or user_id not set, cannot send notification")
            return

        try:
            await self.bot.send_message(
                chat_id=self.user_id,
                text=message,
                parse_mode=parse_mode
            )
            logger.debug(f"Notification sent to user {self.user_id}")
        except Exception as e:
            logger.error(f"Failed to send notification: {e}", exc_info=True)

    async def notify_opportunity_found(
        self,
        symbol: str,
        spread: float,
        long_exchange: str,
        short_exchange: str,
        long_price: float,
        short_price: float
    ):
        """Уведомление о найденной арбитражной возможности"""
        message = (
            f"💰 <b>Арбитражная возможность!</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n"
            f"📊 Спред: <b>{spread:.2f}%</b>\n\n"
            f"📈 LONG {long_exchange.upper()}\n"
            f"   Цена: ${long_price:,.2f}\n\n"
            f"📉 SHORT {short_exchange.upper()}\n"
            f"   Цена: ${short_price:,.2f}\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_position_opened(
        self,
        long_exchange: str,
        short_exchange: str,
        size: float,
        long_price: float,
        short_price: float,
        spread: float
    ):
        """Уведомление об открытии позиции"""
        message = (
            f"✅ <b>Позиция открыта!</b>\n\n"
            f"📈 LONG {long_exchange.upper()}\n"
            f"   Цена входа: ${long_price:,.2f}\n"
            f"   Размер: {size}\n\n"
            f"📉 SHORT {short_exchange.upper()}\n"
            f"   Цена входа: ${short_price:,.2f}\n"
            f"   Размер: {size}\n\n"
            f"📊 Спред: {spread:.3f}%\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_position_closed(
        self,
        pnl: float,
        long_exchange: str,
        short_exchange: str,
        size: float,
        duration_seconds: Optional[float] = None
    ):
        """Уведомление о закрытии позиции"""
        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        pnl_sign = "+" if pnl > 0 else ""

        duration_text = ""
        if duration_seconds:
            if duration_seconds < 60:
                duration_text = f"\n⏱ Длительность: {duration_seconds:.0f}с"
            else:
                minutes = duration_seconds / 60
                duration_text = f"\n⏱ Длительность: {minutes:.1f}м"

        message = (
            f"{pnl_emoji} <b>Позиция закрыта!</b>\n\n"
            f"💰 PnL: <b>{pnl_sign}{pnl:.2f} USDT</b>\n\n"
            f"📈 LONG {long_exchange.upper()}\n"
            f"📉 SHORT {short_exchange.upper()}\n"
            f"💼 Размер: {size} BTC{duration_text}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_error(self, error_type: str, error_message: str):
        """Уведомление об ошибке"""
        message = (
            f"⚠️ <b>Ошибка!</b>\n\n"
            f"Тип: {error_type}\n"
            f"Сообщение: {error_message}\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_execution_failed(
        self,
        reason: str,
        long_exchange: str,
        short_exchange: str
    ):
        """Уведомление о неудачном исполнении"""
        message = (
            f"❌ <b>Не удалось открыть позицию</b>\n\n"
            f"Причина: {reason}\n\n"
            f"📈 LONG {long_exchange.upper()}\n"
            f"📉 SHORT {short_exchange.upper()}\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_hedge_executed(self, exchange: str, reason: str):
        """Уведомление о выполнении хеджа"""
        message = (
            f"🛡 <b>Экстренный хедж!</b>\n\n"
            f"Биржа: {exchange.upper()}\n"
            f"Причина: {reason}\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_bot_started(self, config):
        """Уведомление о запуске бота"""
        mode_emoji = "🔒" if config.mock_mode else "🔓"
        mode_text = "MOCK (безопасно)" if config.mock_mode else "⚠️ REAL"

        message = (
            f"🤖 <b>Арбитражный бот запущен!</b>\n\n"
            f"Режим: {mode_emoji} <b>{mode_text}</b>\n"
            f"Символ: {config.symbol}\n"
            f"Размер позиции: {config.position_size}\n"
            f"Кредитное плечо: {config.leverage}x\n"
            f"Вход: {config.entry_threshold}%\n"
            f"Выход: {config.exit_threshold}%\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_bot_stopped(self, stats: Dict[str, Any]):
        """Уведомление об остановке бота"""
        message = (
            f"🛑 <b>Арбитражный бот остановлен</b>\n\n"
            f"📊 <b>Статистика сессии:</b>\n"
            f"Всего сделок: {stats.get('total_trades', 0)}\n"
            f"Успешных: {stats.get('successful_trades', 0)}\n"
            f"Неудачных: {stats.get('failed_trades', 0)}\n"
            f"Успешность: {stats.get('success_rate', 0):.1f}%\n\n"
            f"💰 Общий PnL: {stats.get('total_pnl', 0):.2f} USDT\n"
            f"💵 Итоговый баланс: {stats.get('total_balance', 0):.2f} USDT\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_daily_summary(self, stats: Dict[str, Any]):
        """Ежедневная сводка"""
        message = (
            f"📈 <b>Дневная сводка</b>\n\n"
            f"Сделок: {stats.get('total_trades', 0)}\n"
            f"Успешных: {stats.get('successful_trades', 0)}\n"
            f"PnL: {stats.get('total_pnl', 0):.2f} USDT\n"
            f"Средний PnL: {stats.get('avg_pnl', 0):.2f} USDT\n\n"
            f"Баланс: {stats.get('total_balance', 0):.2f} USDT\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await self.send(message)

    async def notify_balance_update(
        self,
        okx_balance: float,
        htx_balance: float,
        total_balance: float
    ):
        """Уведомление об обновлении баланса"""
        message = (
            f"💰 <b>Обновление баланса</b>\n\n"
            f"OKX: {okx_balance:,.2f} USDT\n"
            f"HTX: {htx_balance:,.2f} USDT\n"
            f"Всего: <b>{total_balance:,.2f} USDT</b>\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_spread_alert(
        self,
        spread: float,
        exchange1: str,
        exchange2: str,
        price1: float,
        price2: float
    ):
        """Уведомление о высоком спреде (но не входим в позицию)"""
        message = (
            f"👀 <b>Интересный спред!</b>\n\n"
            f"📊 Спред: <b>{spread:.3f}%</b>\n\n"
            f"{exchange1.upper()}: ${price1:,.2f}\n"
            f"{exchange2.upper()}: ${price2:,.2f}\n\n"
            f"<i>Проверяю условия входа...</i>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_opportunity_disappeared(
        self,
        symbol: str,
        initial_spread: float,
        final_spread: float,
        duration_seconds: float,
        long_exchange: str,
        short_exchange: str
    ):
        """Уведомление об исчезновении арбитражной возможности"""
        # Форматируем длительность
        if duration_seconds < 60:
            duration_text = f"{duration_seconds:.0f}с"
        elif duration_seconds < 3600:
            minutes = duration_seconds / 60
            duration_text = f"{minutes:.1f}м"
        else:
            hours = duration_seconds / 3600
            duration_text = f"{hours:.1f}ч"

        # Определяем эмодзи в зависимости от изменения спреда
        if final_spread < 0.15:
            emoji = "📉"
            status = "исчезла"
        elif final_spread < initial_spread * 0.5:
            emoji = "⬇️"
            status = "значительно упала"
        else:
            emoji = "🔄"
            status = "изменилась"

        message = (
            f"{emoji} <b>Возможность {status}</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n\n"
            f"📊 Начальный спред: <b>{initial_spread:.3f}%</b>\n"
            f"📊 Конечный спред: <b>{final_spread:.3f}%</b>\n\n"
            f"📈 LONG {long_exchange.upper()}\n"
            f"📉 SHORT {short_exchange.upper()}\n\n"
            f"⏱ Держалась: <b>{duration_text}</b>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)

    async def notify_opportunity_updated(
        self,
        symbol: str,
        old_spread: float,
        new_spread: float,
        duration_seconds: float,
        long_exchange: str,
        short_exchange: str
    ):
        """Уведомление об обновлении арбитражной возможности"""
        # Форматируем длительность
        if duration_seconds < 60:
            duration_text = f"{duration_seconds:.0f}с"
        elif duration_seconds < 3600:
            minutes = duration_seconds / 60
            duration_text = f"{minutes:.1f}м"
        else:
            hours = duration_seconds / 3600
            duration_text = f"{hours:.1f}ч"

        # Определяем направление изменения
        if new_spread > old_spread:
            emoji = "📈"
            change = "увеличился"
            change_value = new_spread - old_spread
        else:
            emoji = "📉"
            change = "уменьшился"
            change_value = old_spread - new_spread

        message = (
            f"{emoji} <b>Спред {change}</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n\n"
            f"📊 Было: {old_spread:.3f}%\n"
            f"📊 Стало: <b>{new_spread:.3f}%</b>\n"
            f"{'🔺' if new_spread > old_spread else '🔻'} Изменение: {change_value:.3f}%\n\n"
            f"📈 LONG {long_exchange.upper()}\n"
            f"📉 SHORT {short_exchange.upper()}\n\n"
            f"⏱ Держится: <b>{duration_text}</b>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(message)
