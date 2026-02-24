"""
TradeExecutor — исполнение и отслеживание арбитражных сделок.
Поддерживает несколько символов одновременно, dry_run и monitoring_only режимы.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple
from datetime import datetime

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.exchanges import OKXRestClient, HTXRestClient

logger = get_arbitrage_logger("trade_executor")

# Комиссии по умолчанию (перп-фьючерсы)
DEFAULT_FEE_RATE = 0.0006   # 0.06% taker за одну ногу
ROUND_TRIP_FEE_RATE = DEFAULT_FEE_RATE * 4  # 4 ноги = 0.24%


@dataclass
class TradeRecord:
    """Запись об одной арбитражной сделке"""
    # Идентификация
    strategy: str
    symbol: str
    long_exchange: str
    short_exchange: str

    # Параметры входа
    entry_time: float = field(default_factory=time.time)
    entry_long_price: float = 0.0
    entry_short_price: float = 0.0
    size: float = 0.0
    entry_spread_pct: float = 0.0

    # Параметры выхода
    exit_time: Optional[float] = None
    exit_long_price: float = 0.0
    exit_short_price: float = 0.0
    exit_spread_pct: float = 0.0

    # Результаты
    gross_pnl: float = 0.0          # До комиссий
    total_fees: float = 0.0         # Суммарные комиссии в USDT
    net_pnl: float = 0.0            # Чистый P&L
    is_open: bool = True
    dry_run: bool = False           # Была ли сделка реальной

    # ID ордеров
    long_order_id: Optional[str] = None
    short_order_id: Optional[str] = None

    def duration_seconds(self) -> float:
        end = self.exit_time or time.time()
        return end - self.entry_time

    def duration_str(self) -> str:
        secs = int(self.duration_seconds())
        if secs < 60:
            return f"{secs}с"
        elif secs < 3600:
            return f"{secs // 60}м {secs % 60}с"
        else:
            return f"{secs // 3600}ч {(secs % 3600) // 60}м"

    def entry_time_str(self) -> str:
        return datetime.fromtimestamp(self.entry_time).strftime("%H:%M:%S")

    def exit_time_str(self) -> str:
        if self.exit_time:
            return datetime.fromtimestamp(self.exit_time).strftime("%H:%M:%S")
        return "—"


class TradeExecutor:
    """
    Исполняет и отслеживает арбитражные сделки.

    Поддерживает:
    - monitoring_only = True: НЕ торгует, только логирует
    - dry_run_mode = True: симулирует сделки, реальных ордеров нет
    - Реальный режим: размещает ордера на обеих биржах одновременно
    """

    def __init__(
        self,
        config: ArbitrageConfig,
        okx_client: OKXRestClient,
        htx_client: HTXRestClient,
    ):
        self.config = config
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.order_timeout = config.order_timeout_ms / 1000

    async def open_trade(
        self,
        strategy: str,
        symbol: str,
        long_exchange: str,
        short_exchange: str,
        long_price: float,
        short_price: float,
        size: float,
        spread_pct: float,
    ) -> Tuple[bool, Optional[TradeRecord], str]:
        """
        Открыть арбитражную позицию.

        Returns:
            (success, trade_record, message)
        """
        # В режиме monitoring_only торговля отключена
        if self.config.monitoring_only:
            return False, None, "monitoring_only mode — trading disabled"

        logger.info(
            f"[{strategy}] Opening trade {symbol}: "
            f"LONG {long_exchange} @ {long_price:.4f}, "
            f"SHORT {short_exchange} @ {short_price:.4f}, "
            f"size={size}, spread={spread_pct:.3f}%"
        )

        # Создаём запись
        trade = TradeRecord(
            strategy=strategy,
            symbol=symbol,
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            entry_long_price=long_price,
            entry_short_price=short_price,
            size=size,
            entry_spread_pct=spread_pct,
            dry_run=self.config.dry_run_mode,
        )

        if self.config.dry_run_mode:
            # DRY RUN: симулируем успешное открытие
            trade.long_order_id = f"dry_{long_exchange}_{int(time.time())}"
            trade.short_order_id = f"dry_{short_exchange}_{int(time.time())}"
            logger.warning(
                f"[DRY RUN] Trade opened (simulated): {symbol} "
                f"LONG {long_exchange} @ {long_price:.4f}, "
                f"SHORT {short_exchange} @ {short_price:.4f}"
            )
            return True, trade, "DRY RUN: simulated entry"

        # Реальное размещение ордеров — одновременно на обе биржи
        try:
            long_task = self._place_order(long_exchange, symbol, "buy", long_price, size)
            short_task = self._place_order(short_exchange, symbol, "sell", short_price, size)
            results = await asyncio.gather(long_task, short_task, return_exceptions=True)

            long_result, short_result = results
            long_ok = not isinstance(long_result, Exception) and long_result.get("success", False)
            short_ok = not isinstance(short_result, Exception) and short_result.get("success", False)

            if long_ok and short_ok:
                trade.long_order_id = long_result.get("order_id")
                trade.short_order_id = short_result.get("order_id")
                logger.info(f"[{strategy}] Both legs opened for {symbol}")
                return True, trade, "Both positions opened"

            elif long_ok and not short_ok:
                # Аварийное закрытие открытой ноги
                logger.error(f"[{strategy}] Short leg failed for {symbol}, emergency close")
                await self._place_order(long_exchange, symbol, "sell", long_price, size, market=True)
                return False, None, f"Short leg failed: {short_result}"

            elif short_ok and not long_ok:
                logger.error(f"[{strategy}] Long leg failed for {symbol}, emergency close")
                await self._place_order(short_exchange, symbol, "buy", short_price, size, market=True)
                return False, None, f"Long leg failed: {long_result}"

            else:
                return False, None, "Both legs failed"

        except Exception as e:
            logger.error(f"[{strategy}] Entry error for {symbol}: {e}", exc_info=True)
            return False, None, f"Entry error: {str(e)}"

    async def close_trade(
        self,
        trade: TradeRecord,
        exit_long_price: float,
        exit_short_price: float,
        exit_spread_pct: float,
        reason: str = "exit_threshold_reached",
    ) -> Tuple[bool, str]:
        """
        Закрыть арбитражную позицию и рассчитать P&L.

        Returns:
            (success, message)
        """
        if not trade.is_open:
            return False, "Trade already closed"

        logger.info(
            f"[{trade.strategy}] Closing trade {trade.symbol}: "
            f"exit LONG {trade.long_exchange} @ {exit_long_price:.4f}, "
            f"exit SHORT {trade.short_exchange} @ {exit_short_price:.4f}, "
            f"reason={reason}"
        )

        if self.config.dry_run_mode:
            # DRY RUN: симулируем закрытие
            self._finalize_trade(trade, exit_long_price, exit_short_price, exit_spread_pct)
            logger.warning(
                f"[DRY RUN] Trade closed (simulated): {trade.symbol} "
                f"net_pnl={trade.net_pnl:.4f} USDT"
            )
            return True, f"DRY RUN: simulated exit, net PnL={trade.net_pnl:.4f} USDT"

        # Реальное закрытие — закрываем обе ноги одновременно
        try:
            # LONG нужно продать (sell), SHORT нужно выкупить (buy)
            close_long = self._place_order(
                trade.long_exchange, trade.symbol, "sell", exit_long_price, trade.size, market=True
            )
            close_short = self._place_order(
                trade.short_exchange, trade.symbol, "buy", exit_short_price, trade.size, market=True
            )
            results = await asyncio.gather(close_long, close_short, return_exceptions=True)

            long_ok = not isinstance(results[0], Exception) and results[0].get("success", False)
            short_ok = not isinstance(results[1], Exception) and results[1].get("success", False)

            if long_ok and short_ok:
                self._finalize_trade(trade, exit_long_price, exit_short_price, exit_spread_pct)
                logger.info(
                    f"[{trade.strategy}] Trade closed: {trade.symbol} "
                    f"net_pnl={trade.net_pnl:.4f} USDT"
                )
                return True, f"Closed, net PnL={trade.net_pnl:.4f} USDT"
            else:
                logger.error(
                    f"[{trade.strategy}] Partial close for {trade.symbol}: "
                    f"long_ok={long_ok}, short_ok={short_ok}"
                )
                return False, "Partial close — manual intervention needed"

        except Exception as e:
            logger.error(f"[{trade.strategy}] Exit error for {trade.symbol}: {e}", exc_info=True)
            return False, f"Exit error: {str(e)}"

    def _finalize_trade(
        self,
        trade: TradeRecord,
        exit_long_price: float,
        exit_short_price: float,
        exit_spread_pct: float,
    ) -> None:
        """Записать результаты и рассчитать P&L"""
        trade.exit_time = time.time()
        trade.exit_long_price = exit_long_price
        trade.exit_short_price = exit_short_price
        trade.exit_spread_pct = exit_spread_pct
        trade.is_open = False

        # P&L от LONG ноги: (exit - entry) * size
        long_pnl = (exit_long_price - trade.entry_long_price) * trade.size
        # P&L от SHORT ноги: (entry - exit) * size
        short_pnl = (trade.entry_short_price - exit_short_price) * trade.size

        trade.gross_pnl = long_pnl + short_pnl

        # Комиссии: 4 ноги × fee_rate × avg_price × size
        avg_price = (trade.entry_long_price + trade.entry_short_price) / 2
        trade.total_fees = avg_price * trade.size * ROUND_TRIP_FEE_RATE
        trade.net_pnl = trade.gross_pnl - trade.total_fees

    async def _place_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        price: float,
        size: float,
        market: bool = False,
    ) -> Dict[str, Any]:
        """Разместить ордер на бирже"""
        order_type = "market" if market else "limit"
        tif = None if market else "ioc"

        try:
            if exchange == "okx":
                # OKX: symbol формат BTC-USDT-SWAP
                okx_symbol = _to_okx_symbol(symbol)
                kwargs: Dict[str, Any] = dict(
                    symbol=okx_symbol, side=side, size=size, order_type=order_type
                )
                if not market:
                    kwargs["price"] = price
                    kwargs["time_in_force"] = "ioc"

                result = await asyncio.wait_for(
                    self.okx_client.place_order(**kwargs),
                    timeout=self.order_timeout
                )
                if result.get("code") == "0" and result.get("data"):
                    return {"success": True, "order_id": result["data"][0].get("ordId")}
                return {"success": False, "error": result}

            elif exchange == "htx":
                kwargs = dict(
                    symbol=symbol, side=side,
                    size=size, order_type="opponent" if market else "limit",
                    offset="open",
                )
                if not market:
                    kwargs["price"] = price

                result = await asyncio.wait_for(
                    self.htx_client.place_order(**kwargs),
                    timeout=self.order_timeout
                )
                if result.get("status") == "ok" and result.get("data"):
                    return {"success": True, "order_id": str(result["data"].get("order_id", ""))}
                return {"success": False, "error": result}

            else:
                return {"success": False, "error": f"Unknown exchange: {exchange}"}

        except asyncio.TimeoutError:
            logger.error(f"Order timeout: {exchange} {symbol} {side}")
            return {"success": False, "error": "Timeout"}
        except Exception as e:
            logger.error(f"Order error: {exchange} {symbol} {side}: {e}")
            return {"success": False, "error": str(e)}


def _to_okx_symbol(symbol: str) -> str:
    """Конвертировать BTCUSDT → BTC-USDT-SWAP"""
    if "-" in symbol:
        return symbol  # уже в формате OKX
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    return symbol
