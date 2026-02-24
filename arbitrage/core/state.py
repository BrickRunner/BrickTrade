"""
Управление состоянием арбитражного бота
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from datetime import datetime
import asyncio

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("state")


@dataclass
class OrderbookData:
    """Данные стакана биржи"""
    exchange: str
    symbol: str
    bids: list
    asks: list
    timestamp: int
    best_bid: float = 0.0
    best_ask: float = 0.0

    def __post_init__(self):
        if self.bids:
            self.best_bid = float(self.bids[0][0])
        if self.asks:
            self.best_ask = float(self.asks[0][0])


@dataclass
class Position:
    """Информация о позиции"""
    exchange: str
    symbol: str
    side: str  # LONG or SHORT
    size: float
    entry_price: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    order_id: Optional[str] = None


@dataclass
class ArbitrageOpportunity:
    """Арбитражная возможность"""
    spread: float
    long_exchange: str
    short_exchange: str
    long_price: float
    short_price: float
    size: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


class BotState:
    """Состояние арбитражного бота"""

    def __init__(self):
        # Orderbooks
        self.okx_orderbook: Optional[OrderbookData] = None
        self.htx_orderbook: Optional[OrderbookData] = None
        self.orderbook_lock = asyncio.Lock()

        # Positions
        self.positions: Dict[str, Position] = {}
        self.position_lock = asyncio.Lock()

        # Trading state
        self.is_in_position = False
        self.current_opportunity: Optional[ArbitrageOpportunity] = None

        # Balance
        self.okx_balance: float = 0.0
        self.htx_balance: float = 0.0
        self.total_balance: float = 0.0

        # Statistics
        self.total_trades = 0
        self.successful_trades = 0
        self.failed_trades = 0
        self.total_pnl = 0.0

        # Running state
        self.is_running = False
        self.is_connected = {"okx": False, "htx": False}

    async def update_orderbook(self, orderbook: Dict[str, Any]) -> None:
        """Обновить данные стакана"""
        async with self.orderbook_lock:
            exchange = orderbook.get("exchange")

            orderbook_data = OrderbookData(
                exchange=exchange,
                symbol=orderbook.get("symbol", ""),
                bids=orderbook.get("bids", []),
                asks=orderbook.get("asks", []),
                timestamp=orderbook.get("timestamp", 0)
            )

            if exchange == "okx":
                self.okx_orderbook = orderbook_data
                self.is_connected["okx"] = True
            elif exchange == "htx":
                self.htx_orderbook = orderbook_data
                self.is_connected["htx"] = True

            logger.debug(
                f"{exchange.upper()} orderbook updated: "
                f"bid={orderbook_data.best_bid}, ask={orderbook_data.best_ask}"
            )

    async def add_position(self, position: Position) -> None:
        """Добавить позицию"""
        async with self.position_lock:
            key = f"{position.exchange}_{position.symbol}"
            self.positions[key] = position
            self.is_in_position = True
            logger.info(f"Position added: {position}")

    async def remove_position(self, exchange: str, symbol: str) -> Optional[Position]:
        """Удалить позицию"""
        async with self.position_lock:
            key = f"{exchange}_{symbol}"
            position = self.positions.pop(key, None)

            if not self.positions:
                self.is_in_position = False
                self.current_opportunity = None

            if position:
                logger.info(f"Position removed: {position}")

            return position

    async def get_position(self, exchange: str, symbol: str) -> Optional[Position]:
        """Получить позицию"""
        async with self.position_lock:
            key = f"{exchange}_{symbol}"
            return self.positions.get(key)

    async def clear_positions(self) -> None:
        """Очистить все позиции"""
        async with self.position_lock:
            self.positions.clear()
            self.is_in_position = False
            self.current_opportunity = None
            logger.info("All positions cleared")

    def update_balance(self, exchange: str, balance: float) -> None:
        """Обновить баланс биржи"""
        if exchange == "okx":
            self.okx_balance = balance
        elif exchange == "htx":
            self.htx_balance = balance

        self.total_balance = self.okx_balance + self.htx_balance
        logger.debug(f"{exchange.upper()} balance updated: {balance} USDT")

    def record_trade(self, success: bool, pnl: float = 0.0) -> None:
        """Записать результат трейда"""
        self.total_trades += 1
        if success:
            self.successful_trades += 1
            self.total_pnl += pnl
        else:
            self.failed_trades += 1

        logger.info(
            f"Trade recorded: success={success}, pnl={pnl:.2f}, "
            f"total_trades={self.total_trades}, total_pnl={self.total_pnl:.2f}"
        )

    def get_orderbooks(self) -> tuple[Optional[OrderbookData], Optional[OrderbookData]]:
        """Получить текущие стаканы (OKX, HTX)"""
        return self.okx_orderbook, self.htx_orderbook

    def is_both_connected(self) -> bool:
        """Проверить подключение к обеим биржам"""
        return self.is_connected["okx"] and self.is_connected["htx"]

    def calculate_pnl(self) -> float:
        """
        Рассчитать текущий PnL на основе открытых позиций

        Для арбитража PnL это разница между входом и текущим состоянием:
        - LONG позиция: (current_price - entry_price) * size
        - SHORT позиция: (entry_price - current_price) * size

        Возвращает общий PnL в USDT
        """
        if not self.is_in_position or not self.current_opportunity:
            return 0.0

        # Простой расчет на основе entry opportunity
        # В реальности PnL рассчитывается по закрытым позициям
        # Здесь мы используем упрощенную оценку

        # Получаем текущие цены
        okx_ob, htx_ob = self.get_orderbooks()
        if not okx_ob or not htx_ob:
            return 0.0

        try:
            opportunity = self.current_opportunity
            size = opportunity.size

            # Расчет PnL для каждой стороны
            if opportunity.long_exchange == "okx":
                # LONG на OKX (закрываем по bid), SHORT на HTX (закрываем по ask)
                long_pnl = (okx_ob.best_bid - opportunity.long_price) * size
                short_pnl = (opportunity.short_price - htx_ob.best_ask) * size
            else:
                # LONG на HTX (закрываем по bid), SHORT на OKX (закрываем по ask)
                long_pnl = (htx_ob.best_bid - opportunity.long_price) * size
                short_pnl = (opportunity.short_price - okx_ob.best_ask) * size

            total_pnl = long_pnl + short_pnl

            # Учитываем комиссии (примерно 0.05% на каждую сторону, всего 0.2% за полный цикл)
            fee_rate = 0.002  # 0.2%
            avg_price = (opportunity.long_price + opportunity.short_price) / 2
            fees = avg_price * size * fee_rate * 2  # 2 позиции

            return total_pnl - fees

        except Exception as e:
            logger.error(f"Error calculating PnL: {e}", exc_info=True)
            return 0.0

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику бота"""
        return {
            "total_trades": self.total_trades,
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "success_rate": (
                self.successful_trades / self.total_trades * 100
                if self.total_trades > 0 else 0
            ),
            "total_pnl": self.total_pnl,
            "avg_pnl": (
                self.total_pnl / self.successful_trades
                if self.successful_trades > 0 else 0
            ),
            "okx_balance": self.okx_balance,
            "htx_balance": self.htx_balance,
            "total_balance": self.total_balance,
            "in_position": self.is_in_position,
            "positions_count": len(self.positions)
        }
