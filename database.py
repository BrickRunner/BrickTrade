import aiosqlite
import logging
from typing import Optional, List, Tuple
from config import DB_PATH

logger = logging.getLogger(__name__)

# Whitelist для защиты от SQL-инъекций
ALLOWED_SETTINGS_FIELDS = {'currencies', 'notify_time', 'days', 'timezone', 'last_sent_date'}


async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Создание таблиц
        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            currencies TEXT DEFAULT 'USD,EUR',
            notify_time TEXT DEFAULT '08:00',
            days TEXT DEFAULT '1,2,3,4,5',
            timezone TEXT DEFAULT '3',
            last_sent_date TEXT
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS thresholds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            value REAL NOT NULL,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
        );
        """)

        # Создание индексов для оптимизации запросов
        await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_thresholds_user_id ON thresholds(user_id);
        """)

        await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_thresholds_currency ON thresholds(currency);
        """)

        await db.commit()
        logger.info("Database initialized successfully")


async def get_settings(user_id: int) -> Optional[Tuple]:
    """Получение настроек пользователя (создаёт запись при первом обращении)"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT user_id, currencies, notify_time, days, timezone, last_sent_date FROM user_settings WHERE user_id=?",
                (user_id,)
            )
            row = await cur.fetchone()
            if row:
                return row
            # Race-safe bootstrap: INSERT OR IGNORE avoids UNIQUE violation on concurrent /start.
            await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
            await db.commit()
            cur = await db.execute(
                "SELECT user_id, currencies, notify_time, days, timezone, last_sent_date FROM user_settings WHERE user_id=?",
                (user_id,)
            )
            return await cur.fetchone()
    except Exception as e:
        logger.error(f"Error getting settings for user {user_id}: {e}", exc_info=True)
        raise


async def update_settings(user_id: int, field: str, value: str):
    """Обновление настроек пользователя"""
    # Защита от SQL-инъекций: проверка поля по белому списку
    if field not in ALLOWED_SETTINGS_FIELDS:
        logger.error(f"Attempted to update invalid field: {field}")
        raise ValueError(f"Invalid settings field: {field}")

    # Валидация и санитизация значения
    if isinstance(value, str):
        value = value[:500]  # Ограничение длины строки

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Guarantee user row exists before update.
            await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
            # Безопасно: field проверен по белому списку
            await db.execute(f"UPDATE user_settings SET {field}=? WHERE user_id=?", (value, user_id))
            await db.commit()
            logger.info(f"Updated {field} for user {user_id}")
    except Exception as e:
        logger.error(f"Error updating settings for user {user_id}: {e}", exc_info=True)
        raise


async def get_user_thresholds(user_id: int) -> List[Tuple]:
    """Получение пороговых значений пользователя"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, currency, value, comment FROM thresholds WHERE user_id=?",
                (user_id,)
            )
            return await cur.fetchall()
    except Exception as e:
        logger.error(f"Error getting thresholds for user {user_id}: {e}", exc_info=True)
        raise


async def add_threshold(user_id: int, currency: str, value: float, comment: str):
    """Добавление порогового значения"""
    # Валидация и санитизация входных данных
    currency = currency.strip().upper()[:10]  # Ограничение длины
    if comment:
        comment = comment[:200]  # Ограничение длины комментария

    if value <= 0:
        raise ValueError("Threshold value must be positive")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO thresholds (user_id, currency, value, comment) VALUES (?,?,?,?)",
                (user_id, currency, value, comment)
            )
            await db.commit()
            logger.info(f"Added threshold for user {user_id}: {currency} {value}")
    except Exception as e:
        logger.error(f"Error adding threshold for user {user_id}: {e}", exc_info=True)
        raise


async def delete_threshold(threshold_id: int, user_id: int) -> Optional[Tuple[str, float]]:
    """Удаление порогового значения"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT currency, value FROM thresholds WHERE id=? AND user_id=?",
                (threshold_id, user_id)
            )
            row = await cur.fetchone()
            if not row:
                logger.warning(f"Threshold {threshold_id} not found for user {user_id}")
                return None
            currency, value = row
            await db.execute("DELETE FROM thresholds WHERE id=? AND user_id=?", (threshold_id, user_id))
            await db.commit()
            logger.info(f"Deleted threshold {threshold_id} for user {user_id}")
            return (currency, value)
    except Exception as e:
        logger.error(f"Error deleting threshold {threshold_id} for user {user_id}: {e}", exc_info=True)
        raise


async def get_all_users_settings() -> List[Tuple]:
    """Получение настроек всех пользователей для scheduler"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT user_id, currencies, notify_time, days, timezone, last_sent_date FROM user_settings"
            )
            return await cur.fetchall()
    except Exception as e:
        logger.error(f"Error getting all users settings: {e}", exc_info=True)
        raise


async def update_last_sent_date(user_id: int, date_iso: str):
    """Обновление даты последней отправки"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE user_settings SET last_sent_date=? WHERE user_id=?", (date_iso, user_id))
            await db.commit()
    except Exception as e:
        logger.error(f"Error updating last sent date for user {user_id}: {e}", exc_info=True)
        raise
