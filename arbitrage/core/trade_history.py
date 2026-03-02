"""
Сохранение истории сделок в SQLite.
Per-pair статистика, экспорт, auto-blacklist.
"""
import aiosqlite
import os
import time
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("trade_history")

DB_PATH = os.getenv("ARB_DB_PATH", "arbitrage_trades.db")


async def init_trade_db():
    """Создать таблицы для истории сделок"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            long_exchange TEXT NOT NULL,
            short_exchange TEXT NOT NULL,
            entry_spread REAL NOT NULL,
            exit_spread REAL,
            entry_time REAL NOT NULL,
            exit_time REAL,
            duration_seconds REAL,
            size_usd REAL NOT NULL,
            okx_contracts INTEGER,
            htx_contracts INTEGER,
            long_price REAL,
            short_price REAL,
            pnl_usd REAL DEFAULT 0,
            fee_usd REAL DEFAULT 0,
            funding_cost REAL DEFAULT 0,
            slippage_pct REAL DEFAULT 0,
            exit_reason TEXT,
            entry_threshold REAL,
            exit_threshold REAL,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            okx_balance REAL,
            htx_balance REAL,
            total_balance REAL,
            timestamp REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS pair_blacklist (
            symbol TEXT PRIMARY KEY,
            reason TEXT,
            blacklisted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_remove_at REAL
        )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)")

        await db.commit()
    logger.info("Trade history DB initialized")


async def save_trade_open(
    symbol: str, long_exchange: str, short_exchange: str,
    entry_spread: float, size_usd: float,
    okx_contracts: int, htx_contracts: int,
    long_price: float, short_price: float,
    entry_threshold: float, exit_threshold: float
) -> int:
    """Сохранить открытие сделки. Возвращает trade_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO trades
            (symbol, long_exchange, short_exchange, entry_spread, entry_time,
             size_usd, okx_contracts, htx_contracts, long_price, short_price,
             entry_threshold, exit_threshold, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (symbol, long_exchange, short_exchange, entry_spread, time.time(),
             size_usd, okx_contracts, htx_contracts, long_price, short_price,
             entry_threshold, exit_threshold)
        )
        await db.commit()
        trade_id = cur.lastrowid
        logger.info(f"Trade #{trade_id} saved: {symbol} open")
        return trade_id


async def save_trade_close(
    trade_id: int, exit_spread: float, pnl_usd: float,
    fee_usd: float = 0, funding_cost: float = 0,
    slippage_pct: float = 0, exit_reason: str = ""
):
    """Обновить сделку при закрытии"""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем entry_time для расчёта duration
        cur = await db.execute("SELECT entry_time FROM trades WHERE id=?", (trade_id,))
        row = await cur.fetchone()
        duration = now - row[0] if row else 0

        await db.execute(
            """UPDATE trades SET
            exit_spread=?, exit_time=?, duration_seconds=?,
            pnl_usd=?, fee_usd=?, funding_cost=?,
            slippage_pct=?, exit_reason=?, status='closed'
            WHERE id=?""",
            (exit_spread, now, duration, pnl_usd, fee_usd,
             funding_cost, slippage_pct, exit_reason, trade_id)
        )
        await db.commit()
    logger.info(f"Trade #{trade_id} closed: PnL=${pnl_usd:.4f} reason={exit_reason}")


async def get_open_trade() -> Optional[Dict]:
    """Получить открытую сделку (для восстановления после перезапуска)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM trades WHERE status='open' LIMIT 1")
        row = await cur.fetchone()
        if row:
            return dict(row)
    return None


async def get_pair_stats(symbol: Optional[str] = None) -> List[Dict]:
    """Per-pair статистика из истории"""
    async with aiosqlite.connect(DB_PATH) as db:
        if symbol:
            cur = await db.execute("""
                SELECT symbol,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl_usd) as total_pnl,
                    AVG(pnl_usd) as avg_pnl,
                    SUM(fee_usd) as total_fees,
                    SUM(funding_cost) as total_funding,
                    AVG(duration_seconds) as avg_duration,
                    AVG(slippage_pct) as avg_slippage,
                    MIN(pnl_usd) as worst_trade,
                    MAX(pnl_usd) as best_trade
                FROM trades WHERE status='closed' AND symbol=?
                GROUP BY symbol
            """, (symbol,))
        else:
            cur = await db.execute("""
                SELECT symbol,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl_usd) as total_pnl,
                    AVG(pnl_usd) as avg_pnl,
                    SUM(fee_usd) as total_fees,
                    SUM(funding_cost) as total_funding,
                    AVG(duration_seconds) as avg_duration,
                    AVG(slippage_pct) as avg_slippage,
                    MIN(pnl_usd) as worst_trade,
                    MAX(pnl_usd) as best_trade
                FROM trades WHERE status='closed'
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """)

        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]


async def get_overall_stats() -> Dict:
    """Общая статистика по всем сделкам"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl_usd) as total_pnl,
                AVG(pnl_usd) as avg_pnl,
                SUM(fee_usd) as total_fees,
                SUM(funding_cost) as total_funding,
                AVG(duration_seconds) as avg_duration,
                MIN(pnl_usd) as max_drawdown_trade,
                MAX(pnl_usd) as best_trade
            FROM trades WHERE status='closed'
        """)
        row = await cur.fetchone()
        cols = [d[0] for d in cur.description]
        result = dict(zip(cols, row))
        # Null safety
        for k in result:
            if result[k] is None:
                result[k] = 0
        return result


async def get_recent_trades(limit: int = 20) -> List[Dict]:
    """Последние сделки"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT * FROM trades WHERE status='closed'
            ORDER BY exit_time DESC LIMIT ?""",
            (limit,)
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]


async def save_balance_snapshot(okx: float, htx: float, total: float):
    """Сохранить снэпшот баланса"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO balance_snapshots (okx_balance, htx_balance, total_balance, timestamp) VALUES (?,?,?,?)",
            (okx, htx, total, time.time())
        )
        await db.commit()


async def get_balance_history(hours: int = 24) -> List[Dict]:
    """История баланса за N часов"""
    cutoff = time.time() - hours * 3600
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT * FROM balance_snapshots WHERE timestamp > ? ORDER BY timestamp",
            (cutoff,)
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]


# ─── Auto-blacklist ─────────────────────────────────────────────────────

async def check_auto_blacklist(min_trades: int = 5, max_loss_streak: int = 3) -> List[str]:
    """Проверить пары для автоматического блэклиста.
    Критерии: >= min_trades сделок с win_rate < 30% или >= max_loss_streak убыточных подряд"""
    blacklisted = []
    async with aiosqlite.connect(DB_PATH) as db:
        # Пары с низким win_rate
        cur = await db.execute("""
            SELECT symbol,
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
            FROM trades WHERE status='closed'
            GROUP BY symbol
            HAVING total >= ? AND (CAST(wins AS REAL) / total) < 0.3
        """, (min_trades,))
        for row in await cur.fetchall():
            symbol = row[0]
            await add_to_blacklist(symbol, f"Low win rate: {row[2]}/{row[1]}")
            blacklisted.append(symbol)

        # Пары с loss streak
        cur2 = await db.execute("""
            SELECT symbol, pnl_usd FROM trades WHERE status='closed'
            ORDER BY symbol, exit_time DESC
        """)
        rows = await cur2.fetchall()
        # Group by symbol
        from itertools import groupby
        for sym, group in groupby(rows, key=lambda r: r[0]):
            streak = 0
            for _, pnl in group:
                if pnl <= 0:
                    streak += 1
                else:
                    break
            if streak >= max_loss_streak and sym not in blacklisted:
                await add_to_blacklist(sym, f"Loss streak: {streak}")
                blacklisted.append(sym)

    return blacklisted


async def add_to_blacklist(symbol: str, reason: str, hours: float = 24):
    """Добавить пару в блэклист (временно, на N часов)"""
    remove_at = time.time() + hours * 3600
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO pair_blacklist (symbol, reason, auto_remove_at)
            VALUES (?, ?, ?)""",
            (symbol, reason, remove_at)
        )
        await db.commit()
    logger.warning(f"Blacklisted {symbol}: {reason} (for {hours}h)")


async def remove_from_blacklist(symbol: str):
    """Удалить пару из блэклиста"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pair_blacklist WHERE symbol=?", (symbol,))
        await db.commit()
    logger.info(f"Removed {symbol} from blacklist")


async def get_blacklisted_pairs() -> set:
    """Получить текущий блэклист (с учётом expired)"""
    now = time.time()
    result = set()
    async with aiosqlite.connect(DB_PATH) as db:
        # Удаляем истёкшие
        await db.execute("DELETE FROM pair_blacklist WHERE auto_remove_at < ?", (now,))
        await db.commit()

        cur = await db.execute("SELECT symbol FROM pair_blacklist")
        for row in await cur.fetchall():
            result.add(row[0])
    return result


async def export_trades_csv() -> str:
    """Экспорт сделок в CSV строку"""
    import csv
    import io

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time DESC"
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(cols)
    writer.writerows(rows)
    return output.getvalue()
