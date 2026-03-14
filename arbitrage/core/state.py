"""
Global state for the arbitrage trading system.
Tracks balances, positions, and statistics per strategy.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List, Union

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("state")


@dataclass
class Position:
    """Minimal position model for core execution/tests."""
    exchange: str
    symbol: str
    side: str
    size: float
    entry_price: float
    order_id: Optional[str] = None


@dataclass
class ActivePosition:
    """A live arbitrage position (2 legs)."""
    strategy: str
    symbol: str
    long_exchange: str
    short_exchange: str
    long_contracts: float
    short_contracts: float
    long_price: float
    short_price: float
    entry_spread: float
    size_usd: float
    entry_time: float = field(default_factory=time.time)
    target_profit: float = 0.0
    stop_loss: float = 0.0
    accumulated_funding: float = 0.0
    total_fees: float = 0.0
    trade_id: int = 0
    exit_threshold: float = 0.05

    def duration(self) -> float:
        return time.time() - self.entry_time


@dataclass
class OrderBookData:
    """Lightweight orderbook snapshot for core flow."""
    exchange: str
    symbol: str
    bids: List[List[float]]
    asks: List[List[float]]
    timestamp: float
    best_bid: float
    best_ask: float


@dataclass
class ArbitrageOpportunity:
    """Describes a detected arbitrage opportunity."""
    spread: float
    long_exchange: str
    short_exchange: str
    long_price: float
    short_price: float
    size: float


PositionLike = Union[ActivePosition, Position]


class BotState:
    """Central state for the arbitrage bot."""

    def __init__(self):
        # Balances per exchange
        self.balances: Dict[str, float] = {}
        self.total_balance: float = 0.0

        # Active positions (supports core Position + strategy ActivePosition)
        self.positions: Dict[tuple, PositionLike] = {}

        # Latest orderbooks by exchange (core flow)
        self._orderbooks: Dict[str, OrderBookData] = {}

        # Per-strategy stats
        self.strategy_stats: Dict[str, Dict[str, Any]] = {}

        # Global stats
        self.total_trades: int = 0
        self.successful_trades: int = 0
        self.failed_trades: int = 0
        self.total_pnl: float = 0.0

        # Running state
        self.is_running: bool = False
        self.is_in_position: bool = False

        # Current arbitrage opportunity (used by legacy ArbitrageEngine)
        self.current_opportunity: Optional[ArbitrageOpportunity] = None

        # Legacy compat aliases
        self.okx_balance: float = 0.0
        self.htx_balance: float = 0.0
        self.bybit_balance: float = 0.0

    # ─── Balances ─────────────────────────────────────────────────────────

    def update_balance(self, exchange: str, balance: float) -> None:
        self.balances[exchange] = balance
        # Legacy aliases
        if exchange == "okx":
            self.okx_balance = balance
        elif exchange == "htx":
            self.htx_balance = balance
        elif exchange == "bybit":
            self.bybit_balance = balance
        self.total_balance = sum(self.balances.values())

    def get_balance(self, exchange: str) -> float:
        return self.balances.get(exchange, 0.0)

    # ─── Positions ────────────────────────────────────────────────────────

    def add_position(self, pos: PositionLike) -> None:
        if isinstance(pos, ActivePosition):
            key = (pos.strategy, pos.symbol)
            logger.info(
                f"Position added: {pos.strategy} {pos.symbol} "
                f"L:{pos.long_exchange} S:{pos.short_exchange}"
            )
        else:
            key = (pos.exchange, pos.symbol, pos.side)
            logger.info(
                f"Position added: {pos.exchange} {pos.symbol} side={pos.side}"
            )
        self.positions[key] = pos
        self.is_in_position = True

    def remove_position(self, strategy: str, symbol: str) -> Optional[PositionLike]:
        # Strategy-based lookup (ActivePosition)
        key = (strategy, symbol)
        pos = self.positions.pop(key, None)
        if pos:
            logger.info(f"Position removed: {strategy} {symbol}")
            self.is_in_position = len(self.positions) > 0
            return pos

        # Exchange-based lookup (core Position)
        for key, candidate in list(self.positions.items()):
            if isinstance(candidate, Position) and candidate.exchange == strategy and candidate.symbol == symbol:
                self.positions.pop(key, None)
                logger.info(f"Position removed: {strategy} {symbol}")
                self.is_in_position = len(self.positions) > 0
                return candidate

        return None

    def get_position(self, strategy: str, symbol: str) -> Optional[ActivePosition]:
        pos = self.positions.get((strategy, symbol))
        return pos if isinstance(pos, ActivePosition) else None

    def get_positions_by_strategy(self, strategy: str) -> List[ActivePosition]:
        return [
            p for p in self.positions.values()
            if isinstance(p, ActivePosition) and p.strategy == strategy
        ]

    def get_all_positions(self) -> List[PositionLike]:
        return list(self.positions.values())

    def position_count(self) -> int:
        return len(self.positions)

    def has_position_on_symbol(self, symbol: str) -> bool:
        return any(
            getattr(pos, "symbol", None) == symbol
            for pos in self.positions.values()
        )

    def clear_positions(self) -> None:
        self.positions.clear()
        self.is_in_position = False

    async def update_orderbook(self, data: Dict[str, Any]) -> None:
        exchange = data.get("exchange")
        symbol = data.get("symbol")
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        timestamp = float(data.get("timestamp") or time.time())

        if not exchange or not symbol or not bids or not asks:
            return

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])

        self._orderbooks[exchange] = OrderBookData(
            exchange=exchange,
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=timestamp,
            best_bid=best_bid,
            best_ask=best_ask,
        )

    def get_orderbooks(self) -> tuple[Optional[OrderBookData], Optional[OrderBookData]]:
        return self._orderbooks.get("okx"), self._orderbooks.get("htx")

    # ─── Trade Recording ──────────────────────────────────────────────────

    def record_trade(self, strategy: Optional[str] = None, success: bool = False, pnl: float = 0.0) -> None:
        strategy_name = strategy or "arbitrage"
        self.total_trades += 1
        if success:
            self.successful_trades += 1
        else:
            self.failed_trades += 1
        self.total_pnl += pnl

        # Per-strategy
        if strategy_name not in self.strategy_stats:
            self.strategy_stats[strategy_name] = {
                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0
            }
        stats = self.strategy_stats[strategy_name]
        stats["trades"] += 1
        if success:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        stats["pnl"] += pnl

        logger.info(f"Trade [{strategy_name}]: ok={success} pnl={pnl:+.4f} "
                    f"total={self.total_trades} total_pnl={self.total_pnl:+.4f}")

    def calculate_pnl(self) -> float:
        """Calculate PnL from current open positions using orderbook prices."""
        okx_ob = self._orderbooks.get("okx")
        htx_ob = self._orderbooks.get("htx")
        if not okx_ob or not htx_ob:
            return 0.0

        pnl = 0.0
        for pos in self.positions.values():
            if not isinstance(pos, Position):
                continue
            if pos.exchange == "okx":
                exit_price = okx_ob.best_bid if pos.side == "LONG" else okx_ob.best_ask
            else:
                exit_price = htx_ob.best_bid if pos.side == "LONG" else htx_ob.best_ask
            if pos.side == "LONG":
                pnl += (exit_price - pos.entry_price) * pos.size
            else:
                pnl += (pos.entry_price - exit_price) * pos.size
        return pnl

    # ─── Stats ────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
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
                self.total_pnl / self.total_trades
                if self.total_trades > 0 else 0
            ),
            "total_balance": self.total_balance,
            "balances": dict(self.balances),
            "okx_balance": self.okx_balance,
            "htx_balance": self.htx_balance,
            "bybit_balance": self.bybit_balance,
            "open_positions": self.position_count(),
            "strategy_stats": dict(self.strategy_stats),
        }
