"""
Global state for the arbitrage trading system.
Tracks balances, positions, and statistics per strategy.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("state")


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


class BotState:
    """Central state for the arbitrage bot."""

    def __init__(self):
        # Balances per exchange
        self.balances: Dict[str, float] = {}
        self.total_balance: float = 0.0

        # Active positions: {(strategy, symbol): ActivePosition}
        self.positions: Dict[tuple, ActivePosition] = {}

        # Per-strategy stats
        self.strategy_stats: Dict[str, Dict[str, Any]] = {}

        # Global stats
        self.total_trades: int = 0
        self.successful_trades: int = 0
        self.failed_trades: int = 0
        self.total_pnl: float = 0.0

        # Running state
        self.is_running: bool = False

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

    def add_position(self, pos: ActivePosition) -> None:
        key = (pos.strategy, pos.symbol)
        self.positions[key] = pos
        logger.info(f"Position added: {pos.strategy} {pos.symbol} "
                    f"L:{pos.long_exchange} S:{pos.short_exchange}")

    def remove_position(self, strategy: str, symbol: str) -> Optional[ActivePosition]:
        key = (strategy, symbol)
        pos = self.positions.pop(key, None)
        if pos:
            logger.info(f"Position removed: {strategy} {symbol}")
        return pos

    def get_position(self, strategy: str, symbol: str) -> Optional[ActivePosition]:
        return self.positions.get((strategy, symbol))

    def get_positions_by_strategy(self, strategy: str) -> List[ActivePosition]:
        return [p for (s, _), p in self.positions.items() if s == strategy]

    def get_all_positions(self) -> List[ActivePosition]:
        return list(self.positions.values())

    def position_count(self) -> int:
        return len(self.positions)

    def has_position_on_symbol(self, symbol: str) -> bool:
        return any(sym == symbol for (_, sym) in self.positions)

    # ─── Trade Recording ──────────────────────────────────────────────────

    def record_trade(self, strategy: str, success: bool, pnl: float = 0.0) -> None:
        self.total_trades += 1
        if success:
            self.successful_trades += 1
        else:
            self.failed_trades += 1
        self.total_pnl += pnl

        # Per-strategy
        if strategy not in self.strategy_stats:
            self.strategy_stats[strategy] = {
                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0
            }
        stats = self.strategy_stats[strategy]
        stats["trades"] += 1
        if success:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        stats["pnl"] += pnl

        logger.info(f"Trade [{strategy}]: ok={success} pnl={pnl:+.4f} "
                    f"total={self.total_trades} total_pnl={self.total_pnl:+.4f}")

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
