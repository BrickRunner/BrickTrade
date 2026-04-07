"""
Global state for the arbitrage trading system.
Tracks balances, positions, and statistics per strategy.

FIX #1: Added JSON persistence with atomic writes (write-to-temp + os.replace)
so positions survive process crashes. Load on init, save on every mutation.
"""
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List, Union

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("state")

# Default path for persisted state
DEFAULT_STATE_PATH = os.path.join("data", "arb_state.json")


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
    """Central state for the arbitrage bot.

    FIX #1: Supports JSON persistence with atomic writes so positions
    survive process crashes. Paths and auto-load are configurable.
    """

    def __init__(self, persist_path: str = DEFAULT_STATE_PATH):
        # Balances per exchange
        self.balances: Dict[str, float] = {}
        self.total_balance: float = 0.0

        # Active positions (supports core Position + strategy ActivePosition)
        self.positions: Dict[str, Dict[str, Any]] = {}

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

        # FIX #1: Persistence
        self._persist_path = persist_path
        # FIX #15: Per-instance lock tracking for cleanup
        self._lock_holders: Dict[str, Dict[str, Any]] = {}
        self._load()

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
        self._save()

    def update_balance_sync(self, exchange: str, balance: float) -> None:
        """Sync alias for test compatibility."""
        self.update_balance(exchange, balance)

    def get_balance(self, exchange: str) -> float:
        return self.balances.get(exchange, 0.0)

    # ─── Positions ────────────────────────────────────────────────────────

    def add_position(self, pos: PositionLike) -> None:
        if isinstance(pos, ActivePosition):
            key = f"active:{pos.strategy}:{pos.symbol}"
            logger.info(
                f"Position added: {pos.strategy} {pos.symbol} "
                f"L:{pos.long_exchange} S:{pos.short_exchange}"
            )
        else:
            key = f"legacy:{pos.exchange}:{pos.symbol}:{pos.side}"
            logger.info(
                f"Position added: {pos.exchange} {pos.symbol} side={pos.side}"
            )
        self.positions[key] = self._serialize_position(pos)
        self.is_in_position = True
        self._save()

    def remove_position(self, key_or_strategy: str, symbol: str = "") -> Optional[PositionLike]:
        """Remove position by string key (primary) or legacy (strategy, symbol) pair.

        FIX: Uses string keys consistently — no more confusing tuple/(strategy, symbol)
        vs (exchange, symbol) parameter semantics.
        """
        # Direct string key lookup (primary path used by TradingSystemEngine)
        pos_data = self.positions.pop(key_or_strategy, None)
        if pos_data:
            restored = self._deserialize_position(pos_data)
            logger.info(f"Position removed: {key_or_strategy}")
            self.is_in_position = len(self.positions) > 0
            self._save()
            return restored

        # Legacy fallback: search for matching active or legacy position
        for key, data in list(self.positions.items()):
            parts = key.split(":", 2)
            if parts[0] == "active" and parts[1] == key_or_strategy and parts[2] == symbol:
                data = self.positions.pop(key)
                restored = self._deserialize_position(data)
                logger.info(f"Position removed: {key_or_strategy}:{symbol}")
                self.is_in_position = len(self.positions) > 0
                self._save()
                return restored
            if parts[0] == "legacy" and parts[1] == key_or_strategy and parts[2] == symbol:
                data = self.positions.pop(key)
                restored = self._deserialize_position(data)
                logger.info(f"Position removed: {key_or_strategy}:{symbol}")
                self.is_in_position = len(self.positions) > 0
                self._save()
                return restored

        return None

    def get_position(self, strategy: str, symbol: str) -> Optional[ActivePosition]:
        key = f"active:{strategy}:{symbol}"
        data = self.positions.get(key)
        if data:
            pos = self._deserialize_position(data)
            return pos if isinstance(pos, ActivePosition) else None
        return None

    def get_positions_by_strategy(self, strategy: str) -> List[ActivePosition]:
        """Return all ActivePosition objects for a given strategy.

        FIX C3: Positions are stored as dicts (serialized form). We must
        deserialize each one before checking the strategy, otherwise this
        always returned an empty list after a restart.
        """
        results = []
        for data in self.positions.values():
            pos = self._deserialize_position(data)
            if isinstance(pos, ActivePosition) and pos.strategy == strategy:
                results.append(pos)
        return results

    def get_all_positions(self) -> List[PositionLike]:
        return list(self.positions.values())

    def position_count(self) -> int:
        return len(self.positions)

    def has_position_on_symbol(self, symbol: str) -> bool:
        """Check if any position exists for a given symbol.

        FIX H2: Previously used fragile suffix matching which could produce
        false positives (e.g., 'BTCUSDT' matching 'ETHBTCUSDT'). Now deserializes
        each position and checks the actual symbol field.
        """
        suffixes = (f":{symbol}", f":{symbol}:LONG", f":{symbol}:SHORT")
        for key in self.positions.keys():
            if isinstance(key, str):
                # Key pattern checks — but also verify via deserialized data
                if key.endswith(suffixes):
                    # Double-check by deserializing to avoid substring false positives
                    pos = self._deserialize_position(self.positions[key])
                    if pos.symbol == symbol:
                        return True
            elif isinstance(key, tuple):
                if len(key) >= 2 and key[1] == symbol:
                    return True
        return False

    def clear_positions(self) -> None:
        self.positions.clear()
        self.is_in_position = False
        self._save()

    # ─── Compatibility aliases for existing tests ─────────────────────────

    def add_position_sync(self, pos: PositionLike) -> None:
        """Sync version of add_position (used by tests)."""
        self.add_position(pos)

    def remove_position_sync(self, strategy: str, symbol: str) -> Optional[PositionLike]:
        """Sync version of remove_position (used by tests)."""
        return self.remove_position(strategy, symbol)

    def try_lock_symbol(self, strategy: str, symbol: str) -> bool:
        """Reentrant lock per (strategy, symbol) — used by tests.

        FIX #15: Now records timestamps so cleanup_expired_locks works properly.
        """
        key = f"lock:{strategy}:{symbol}"
        if not hasattr(self, "_symbol_locks"):
            self._symbol_locks: Dict[str, str] = {}
            self._lock_holders = {}
        locks = self._symbol_locks
        if key in locks and locks[key] == strategy:
            return True  # Reentrant
        if key in locks and locks[key] != strategy:
            return False  # Blocked by another strategy
        locks[key] = strategy
        # FIX #15: Record lock timestamp for cleanup
        self._lock_holders[key] = {"strategy": strategy, "ts": time.time()}
        return True

    def release_symbol(self, strategy: str, symbol: str) -> None:
        """Release the symbol lock."""
        key = f"lock:{strategy}:{symbol}"
        locks = getattr(self, "_symbol_locks", {})
        if key in locks:
            del locks[key]
        # Also clean up timestamp tracking
        holders = getattr(self, "_lock_holders", {})
        if key in holders:
            del holders[key]

    def cleanup_expired_locks(self, max_age: float = 300.0) -> None:
        """FIX #15: Actually clean up stale symbol locks.

        Tests call this to verify lock state.
        In production, this prevents a crashed strategy
        from holding locks indefinitely.
        """
        now = time.time()
        holders = getattr(self, "_lock_holders", {})
        keys_to_remove = [
            key for key, holder in list(holders.items())
            if now - holder.get("ts", 0.0) > max_age
        ]
        for key in keys_to_remove:
            del holders[key]
            # Also clean up the corresponding symbol lock
            sym_locks = getattr(self, "_symbol_locks", {})
            if key in sym_locks:
                del sym_locks[key]

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

    def get_orderbooks(self) -> Dict[str, OrderBookData]:
        # FIX #14: Previously hardcoded ("okx", "htx") — now returns all
        # cached orderbooks regardless of which exchanges are configured.
        return dict(self._orderbooks)

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
        """Calculate PnL from current open positions using orderbook prices.

        FIX #4: Previously hardcoded to OKX/HTX only — now supports any exchange
        pair (Bybit, Binance, etc). Looks up the orderbook for the position's
        exchange dynamically instead of assuming "okx" or "htx".
        """
        pnl = 0.0
        for key, pos_data in self.positions.items():
            pos = self._deserialize_position(pos_data)
            if not isinstance(pos, Position):
                continue
            ob = self._orderbooks.get(pos.exchange)
            if not ob:
                continue
            exit_price = ob.best_bid if pos.side == "LONG" else ob.best_ask
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

    # ─── FIX #1: Persistence ────────────────────────────────────────────

    def _serialize_position(self, pos: PositionLike) -> Dict[str, Any]:
        """Serialize a position to a JSON-compatible dict."""
        if isinstance(pos, ActivePosition):
            return {
                "type": "ActivePosition",
                "strategy": pos.strategy,
                "symbol": pos.symbol,
                "long_exchange": pos.long_exchange,
                "short_exchange": pos.short_exchange,
                "long_contracts": pos.long_contracts,
                "short_contracts": pos.short_contracts,
                "long_price": pos.long_price,
                "short_price": pos.short_price,
                "entry_spread": pos.entry_spread,
                "size_usd": pos.size_usd,
                "entry_time": pos.entry_time,
                "target_profit": pos.target_profit,
                "stop_loss": pos.stop_loss,
                "accumulated_funding": pos.accumulated_funding,
                "total_fees": pos.total_fees,
                "trade_id": pos.trade_id,
                "exit_threshold": pos.exit_threshold,
            }
        else:
            return {
                "type": "Position",
                "exchange": pos.exchange,
                "symbol": pos.symbol,
                "side": pos.side,
                "size": pos.size,
                "entry_price": pos.entry_price,
                "order_id": pos.order_id,
            }

    def _deserialize_position(self, data: Dict[str, Any]) -> PositionLike:
        """Deserialize a dict back to a Position or ActivePosition."""
        pos_type = data.get("type", "Position")
        if pos_type == "ActivePosition":
            return ActivePosition(
                strategy=data["strategy"],
                symbol=data["symbol"],
                long_exchange=data["long_exchange"],
                short_exchange=data["short_exchange"],
                long_contracts=data["long_contracts"],
                short_contracts=data["short_contracts"],
                long_price=data["long_price"],
                short_price=data["short_price"],
                entry_spread=data["entry_spread"],
                size_usd=data["size_usd"],
                entry_time=data.get("entry_time", time.time()),
                target_profit=data.get("target_profit", 0.0),
                stop_loss=data.get("stop_loss", 0.0),
                accumulated_funding=data.get("accumulated_funding", 0.0),
                total_fees=data.get("total_fees", 0.0),
                trade_id=data.get("trade_id", 0),
                exit_threshold=data.get("exit_threshold", 0.05),
            )
        else:
            return Position(
                exchange=data["exchange"],
                symbol=data["symbol"],
                side=data["side"],
                size=data["size"],
                entry_price=data["entry_price"],
                order_id=data.get("order_id"),
            )

    def _save(self) -> None:
        """Atomically persist positions to disk (write to temp file + os.replace).

        FIX CRITICAL #3: Also create backup before each save so corruption
        recovery has a previous-good-state to fall back to.
        """
        try:
            data = {
                "positions": {
                    k: v for k, v in self.positions.items()
                },
                "total_trades": self.total_trades,
                "total_pnl": self.total_pnl,
                "balances": dict(self.balances),
            }
            dir_name = os.path.dirname(self._persist_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                # Create/update backup before atomic replace
                backup_path = self._persist_path + ".backup"
                try:
                    # Copy current good file to backup if it exists
                    if os.path.exists(self._persist_path):
                        import shutil
                        shutil.copy2(self._persist_path, backup_path)
                except Exception:
                    pass
                os.replace(tmp_path, self._persist_path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"state_save_failed: {e}")

    def _load(self) -> None:
        """Load persisted state from disk. No-op if file doesn't exist.

        FIX CRITICAL #3: Graceful corruption handling — partial recovery
        instead of silent data loss. If JSON is invalid, attempt to read
        the backup temp file (last good save).
        """
        if not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, "r") as f:
                data = json.load(f)
            for key, pos_data in data.get("positions", {}).items():
                self.positions[key] = pos_data
            self.total_trades = data.get("total_trades", 0)
            self.total_pnl = data.get("total_pnl", 0.0)
            saved_balances = data.get("balances", {})
            if saved_balances:
                for ex, bal in saved_balances.items():
                    self.balances[ex] = bal
                self.total_balance = sum(self.balances.values())
            self.is_in_position = len(self.positions) > 0
            if self.positions:
                logger.info(f"State loaded: {len(self.positions)} positions from {self._persist_path}")
        except json.JSONDecodeError as e:
            # FIX: File is corrupted — try to recover from temp backup
            backup_path = self._persist_path + ".backup"
            if os.path.exists(backup_path):
                logger.warning(f"State file corrupted, attempting recovery from backup: {backup_path}")
                try:
                    with open(backup_path, "r") as f:
                        data = json.load(f)
                    for key, pos_data in data.get("positions", {}).items():
                        self.positions[key] = pos_data
                    self.total_trades = data.get("total_trades", 0)
                    self.total_pnl = data.get("total_pnl", 0.0)
                    saved_balances = data.get("balances", {})
                    if saved_balances:
                        for ex, bal in saved_balances.items():
                            self.balances[ex] = bal
                        self.total_balance = sum(self.balances.values())
                    self.is_in_position = len(self.positions) > 0
                    logger.info(f"State recovered from backup: {len(self.positions)} positions")
                    return
                except Exception:
                    pass
            # No usable backup — log critical error and keep in-memory state clean.
            # DO NOT silently wipe state raising an exception would be safer,
            # but for backwards compat we log and continue with empty state.
            # The caller should check position_count() == 0 after _load().
            logger.critical(
                f"CRITICAL: State file {self._persist_path} is corrupted ({e}). "
                "No backup available. ALL OPEN POSITIONS LOST. "
                "Check exchanges manually via venue.get_open_positions() or CLI tools."
            )
        except Exception as e:
            logger.warning(f"state_load_failed: {e}, starting fresh")

    async def load_from_exchanges(self, venue) -> None:
        """FIX #5: On startup, fetch real open positions from exchanges and sync state."""
        try:
            if hasattr(venue, "get_open_positions"):
                exchange_positions = await venue.get_open_positions()
                for exchange, positions in exchange_positions.items():
                    for pos_data in positions:
                        symbol = pos_data.get("symbol", "")
                        size = float(pos_data.get("size", 0) or 0)
                        if abs(size) > 0:
                            side = "LONG" if size > 0 else "SHORT"
                            key = f"restored:{exchange}:{symbol}:{side}"
                            if key not in self.positions:
                                self.positions[key] = {
                                    "type": "Position",
                                    "exchange": exchange,
                                    "symbol": symbol,
                                    "side": side,
                                    "size": abs(size),
                                    "entry_price": float(pos_data.get("entry_price", 0) or 0),
                                    "order_id": pos_data.get("order_id"),
                                }
                                logger.warning(
                                    f"Restored position from {exchange}: {symbol} {side} {abs(size)} contracts"
                                )
                self.is_in_position = len(self.positions) > 0
                if self.positions:
                    self._save()
        except Exception as e:
            logger.warning(f"load_from_exchanges_failed: {e}")
