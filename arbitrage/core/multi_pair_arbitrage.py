"""
Мульти-парный арбитражный движок.
Мониторинг + автоматическая торговля на реальных биржах.
Поддержка 3 бирж: OKX, HTX, Bybit (все пары бирж параллельно).

Включает:
- Adaptive mean-reversion пороги (per-pair)
- Funding rate учёт в PnL
- Восстановление позиций после перезапуска
- Трекинг слиппеджа и реальных комиссий
- Динамический размер позиции
- Защита от ликвидации
- Auto-blacklist убыточных пар
- Spot-futures basis мониторинг
- Уведомления о балансе
- 3-way arbitrage: OKX↔HTX, OKX↔Bybit, HTX↔Bybit
"""
import asyncio
import math
from typing import Dict, List, Optional, Set, Tuple, Any
import time
from datetime import datetime
from dataclasses import dataclass, field

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig, calculate_spread
from arbitrage.core.state import BotState
from arbitrage.core.risk import RiskManager
from arbitrage.core.execution import ExecutionManager
from arbitrage.core.notifications import NotificationManager
from arbitrage.exchanges import OKXRestClient, HTXRestClient, BybitRestClient

logger = get_arbitrage_logger("multi_pair_arbitrage")


@dataclass
class PairSpread:
    """Информация о спреде для пары"""
    symbol: str
    spread: float
    long_exchange: str   # "okx", "htx", "bybit"
    short_exchange: str  # "okx", "htx", "bybit"
    long_price: float    # ask price on long exchange
    short_price: float   # bid price on short exchange
    timestamp: datetime = field(default_factory=lambda: datetime.now())


@dataclass
class ActiveTrade:
    """Активная арбитражная сделка"""
    symbol: str
    long_exchange: str
    short_exchange: str
    long_price: float
    short_price: float
    entry_spread: float
    size_usd: float
    long_contracts: int
    short_contracts: int
    entry_time: float
    dynamic_exit_threshold: float = 0.05
    trade_id: int = 0
    actual_long_fill_price: float = 0
    actual_short_fill_price: float = 0


@dataclass
class BasisSpread:
    """Спред spot vs futures"""
    symbol: str
    spot_price: float
    futures_price: float
    basis_pct: float
    exchange: str


POPULAR_PAIRS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "LTCUSDT", "MATICUSDT", "UNIUSDT", "ATOMUSDT", "FILUSDT",
    "TRXUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "SUIUSDT", "PEPEUSDT", "SHIBUSDT", "ICPUSDT", "INJUSDT",
    "TONUSDT", "BCHUSDT", "ETCUSDT", "MKRUSDT", "AAVEUSDT",
}


class MultiPairArbitrageEngine:
    """
    Движок мульти-парного арбитража.
    Мониторит спреды на всех парах между всеми биржами (OKX, HTX, Bybit).
    Торгует при подтверждённых возможностях.
    """

    def __init__(
        self,
        config: ArbitrageConfig,
        state: BotState,
        risk_manager: RiskManager,
        execution_manager: ExecutionManager,
        okx_client: OKXRestClient,
        htx_client: HTXRestClient,
        bybit_client: Optional[BybitRestClient] = None,
        notification_manager: Optional[NotificationManager] = None
    ):
        self.config = config
        self.state = state
        self.risk = risk_manager
        self.execution = execution_manager
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.bybit_client = bybit_client
        self.notifications = notification_manager or NotificationManager()

        # Exchange registry
        self.exchanges: Dict[str, Any] = {"okx": okx_client, "htx": htx_client}
        if bybit_client:
            self.exchanges["bybit"] = bybit_client

        # Цены по биржам: {"okx": {"BTCUSDT": {"bid": X, "ask": Y}}, ...}
        self.exchange_prices: Dict[str, Dict[str, Dict[str, float]]] = {
            ex: {} for ex in self.exchanges
        }

        # Contract sizes по биржам
        self.contract_sizes: Dict[str, Dict[str, float]] = {
            ex: {} for ex in self.exchanges
        }

        self.monitored_pairs: Set[str] = set()
        self.best_spreads: List[PairSpread] = []
        self.active_opportunities: Dict[str, Tuple[PairSpread, float, float]] = {}

        self.last_update_time = 0
        self.update_interval = config.update_interval
        self.min_volume_usdt = 10000
        self.min_spread = config.min_spread
        self.spread_change_threshold = config.spread_change_threshold

        self.active_trade: Optional[ActiveTrade] = None
        self.can_trade: bool = (
            not config.dry_run_mode
            and not config.monitoring_only
            and not config.mock_mode
        )

        # Circuit breakers per exchange
        self._circuit_breakers: Dict[str, Dict] = {}
        for ex in self.exchanges:
            if ex != "okx":  # OKX is always "reliable" leg
                self._circuit_breakers[ex] = {
                    "consecutive_failures": 0,
                    "breaker_until": 0,
                    "max_failures": 2,
                    "breaker_seconds": 300,
                }

        self._stop_loss_pct: float = 1.0
        self._max_trade_duration: float = 900
        self._min_pullback_from_peak: float = 0.01  # Reduced: was 0.03 — too restrictive

        # Adaptive spread stats
        self._spread_history: Dict[str, List[Tuple[float, float]]] = {}
        self._spread_history_window: float = 600
        self._pair_stats: Dict[str, Dict] = {}
        self._min_volume_usd: float = 50_000

        # ─── Funding rates ───
        self._funding_rates: Dict[str, Dict[str, float]] = {}
        self._last_funding_update: float = 0

        # ─── Balance monitoring ───
        self._last_balance_notification: float = 0
        self._balance_notification_interval: float = 3600
        self._prev_balance: float = 0
        self._initial_balance: float = 0
        self._balance_alert_threshold_pct: float = 10

        # ─── Spot-futures basis ───
        self._basis_spreads: List[BasisSpread] = []

        # ─── Blacklist ───
        self._blacklisted_pairs: Set[str] = set()
        self._last_blacklist_check: float = 0

        # ─── Trade history ───
        self._trade_db_initialized: bool = False

        # ─── Dynamic sizing ───
        self._consecutive_wins: int = 0
        self._consecutive_losses: int = 0

        # ─── Runtime settings (changeable via Telegram) ───
        self.runtime_settings: Dict[str, float] = {
            "entry_threshold": config.entry_threshold,
            "exit_threshold": config.exit_threshold,
            "max_position_pct": config.max_position_pct,
            "leverage": config.leverage,
            "stop_loss_pct": self._stop_loss_pct,
        }

    # ─── Инициализация ───────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Инициализация — пары, контракты, восстановление позиций"""
        exchange_names = list(self.exchanges.keys())
        logger.info(f"Initializing multi-pair arbitrage engine ({' <-> '.join(e.upper() for e in exchange_names)})")

        try:
            from arbitrage.core.trade_history import init_trade_db
            await init_trade_db()
            self._trade_db_initialized = True
        except Exception as e:
            logger.warning(f"Trade history DB init failed (non-critical): {e}")

        try:
            # Fetch instruments from all exchanges in parallel
            tasks = {}
            for ex in self.exchanges:
                tasks[ex] = self._get_instruments(ex)
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            instruments_per_exchange: Dict[str, Set[str]] = {}
            for ex, result in zip(tasks.keys(), results):
                if isinstance(result, Exception):
                    logger.error(f"{ex.upper()} instruments error: {result}")
                    instruments_per_exchange[ex] = set()
                else:
                    instruments_per_exchange[ex] = result

            for ex, insts in instruments_per_exchange.items():
                logger.info(f"{ex.upper()}: {len(insts)} instruments")

            # Common pairs = intersection of at least 2 exchanges
            all_pairs: Set[str] = set()
            exchange_list = list(instruments_per_exchange.keys())
            for i, ex1 in enumerate(exchange_list):
                for ex2 in exchange_list[i+1:]:
                    common = instruments_per_exchange[ex1] & instruments_per_exchange[ex2]
                    all_pairs |= common

            logger.info(f"Total pairs across exchange pairs: {len(all_pairs)}")

            self.monitored_pairs = await self._filter_pairs(all_pairs)
            logger.info(f"Monitoring {len(self.monitored_pairs)} pairs")

            if self.can_trade:
                # Check that at least one non-OKX exchange has API keys
                has_tradeable_exchange = False
                for ex_name, client in self.exchanges.items():
                    if ex_name == "okx":
                        continue
                    if not getattr(client, 'public_only', True):
                        has_tradeable_exchange = True
                        break

                if not has_tradeable_exchange:
                    logger.warning("No exchange with API keys (besides OKX) — trading DISABLED")
                    self.can_trade = False
                else:
                    okx_ready = await self._check_okx_account_mode()
                    if not okx_ready:
                        self.can_trade = False
                    else:
                        tradeable = sum(1 for s in self.monitored_pairs
                                        if any(self.contract_sizes.get(ex, {}).get(s, 0) > 0
                                               for ex in self.exchanges))
                        logger.info(f"Trading ENABLED: {tradeable} tradeable pairs")

            if self.can_trade:
                await self._recover_positions()

            await self._load_blacklist()
            await self._update_funding_rates()

        except Exception as e:
            logger.error(f"Initialization error: {e}", exc_info=True)
            raise

    # ─── Instruments (universal) ──────────────────────────────────────────

    async def _get_instruments(self, exchange: str) -> Set[str]:
        """Get instruments for any exchange"""
        try:
            if exchange == "okx":
                return await self._get_okx_instruments()
            elif exchange == "htx":
                return await self._get_htx_instruments()
            elif exchange == "bybit":
                return await self._get_bybit_instruments()
            return set()
        except Exception as e:
            logger.error(f"{exchange.upper()} instruments error: {e}")
            return set()

    async def _get_okx_instruments(self) -> Set[str]:
        try:
            result = await self.okx_client.get_instruments(inst_type="SWAP")
            if result.get("code") != "0":
                return set()
            instruments = set()
            for inst in result.get("data", []):
                inst_id = inst.get("instId", "")
                state = inst.get("state", "")
                if state == "live" and "-USDT-SWAP" in inst_id:
                    symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                    instruments.add(symbol)
                    try:
                        ct_val = float(inst.get("ctVal", 0))
                        if ct_val > 0:
                            self.contract_sizes.setdefault("okx", {})[symbol] = ct_val
                    except (ValueError, TypeError):
                        pass
            return instruments
        except Exception as e:
            logger.error(f"OKX instruments error: {e}")
            return set()

    async def _get_htx_instruments(self) -> Set[str]:
        try:
            result = await self.htx_client.get_instruments()
            if result.get("status") != "ok":
                return set()
            instruments = set()
            for inst in result.get("data", []):
                contract_code = inst.get("contract_code", "")
                contract_status = inst.get("contract_status", 0)
                if contract_status == 1 and contract_code.endswith("-USDT"):
                    symbol = contract_code.replace("-", "")
                    instruments.add(symbol)
                    try:
                        ct_size = float(inst.get("contract_size", 0))
                        if ct_size > 0:
                            self.contract_sizes.setdefault("htx", {})[symbol] = ct_size
                    except (ValueError, TypeError):
                        pass
            return instruments
        except Exception as e:
            logger.error(f"HTX instruments error: {e}")
            return set()

    async def _get_bybit_instruments(self) -> Set[str]:
        if not self.bybit_client:
            return set()
        try:
            result = await self.bybit_client.get_instruments()
            if result.get("retCode") != 0:
                return set()
            instruments = set()
            for inst in result.get("result", {}).get("list", []):
                symbol = inst.get("symbol", "")
                status = inst.get("status", "")
                if status == "Trading" and symbol.endswith("USDT"):
                    instruments.add(symbol)
                    try:
                        qty_step = float(inst.get("lotSizeFilter", {}).get("qtyStep", 0))
                        if qty_step > 0:
                            self.contract_sizes.setdefault("bybit", {})[symbol] = qty_step
                    except (ValueError, TypeError):
                        pass
            return instruments
        except Exception as e:
            logger.error(f"Bybit instruments error: {e}")
            return set()

    # ─── Восстановление позиций после перезапуска ────────────────────

    async def _recover_positions(self) -> None:
        """Проверить открытые позиции на биржах и восстановить active_trade"""
        try:
            from arbitrage.core.trade_history import get_open_trade, save_trade_close

            open_trade = await get_open_trade() if self._trade_db_initialized else None
            if not open_trade:
                return

            symbol = open_trade["symbol"]
            long_ex = open_trade["long_exchange"]
            short_ex = open_trade["short_exchange"]
            logger.warning(f"Found open trade in DB: #{open_trade['id']} {symbol} L:{long_ex} S:{short_ex}")

            # Check positions on both exchanges
            long_has_pos = await self._check_exchange_position(long_ex, symbol)
            short_has_pos = await self._check_exchange_position(short_ex, symbol)

            if long_has_pos and short_has_pos:
                self.active_trade = ActiveTrade(
                    symbol=symbol,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    long_price=open_trade["long_price"],
                    short_price=open_trade["short_price"],
                    entry_spread=open_trade["entry_spread"],
                    size_usd=open_trade["size_usd"],
                    long_contracts=open_trade.get("okx_contracts", 0) if long_ex == "okx" else open_trade.get("htx_contracts", 0),
                    short_contracts=open_trade.get("okx_contracts", 0) if short_ex == "okx" else open_trade.get("htx_contracts", 0),
                    entry_time=open_trade["entry_time"],
                    dynamic_exit_threshold=open_trade.get("exit_threshold", self.config.exit_threshold),
                    trade_id=open_trade["id"]
                )
                logger.warning(f"RECOVERED trade: {symbol} #{open_trade['id']}")
                await self.notifications.send(
                    f"🔄 <b>Позиция восстановлена</b>\n\n"
                    f"Пара: {symbol}\n"
                    f"L:{long_ex.upper()} S:{short_ex.upper()}\n"
                    f"Спред входа: {open_trade['entry_spread']:.3f}%"
                )
            elif long_has_pos or short_has_pos:
                side = long_ex.upper() if long_has_pos else short_ex.upper()
                logger.error(f"ORPHANED position on {side} for {symbol}!")
                await self.notifications.send(
                    f"⚠️ <b>Осиротевшая позиция!</b>\n\n"
                    f"Пара: {symbol}\nБиржа: {side}\n"
                    f"Требуется ручное закрытие!"
                )
            else:
                await save_trade_close(open_trade["id"], 0, 0, exit_reason="not_found_on_restart")
                logger.info(f"Closed stale trade #{open_trade['id']}")

        except Exception as e:
            logger.error(f"Position recovery error: {e}", exc_info=True)

    async def _check_exchange_position(self, exchange: str, symbol: str) -> bool:
        """Check if exchange has an open position for symbol"""
        try:
            if exchange == "okx":
                result = await self.okx_client.get_positions()
                if result.get("code") == "0":
                    for pos in result.get("data", []):
                        inst_id = pos.get("instId", "")
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        if sym == symbol and float(pos.get("pos", 0)) != 0:
                            return True
            elif exchange == "htx":
                result = await self.htx_client.get_cross_position(symbol)
                if result.get("status") == "ok":
                    for pos in result.get("data", []):
                        if float(pos.get("volume", 0)) > 0:
                            return True
            elif exchange == "bybit":
                if self.bybit_client:
                    result = await self.bybit_client.get_cross_position(symbol)
                    if result.get("retCode") == 0:
                        for pos in result.get("result", {}).get("list", []):
                            if float(pos.get("size", 0)) > 0:
                                return True
        except Exception as e:
            logger.error(f"{exchange.upper()} position check failed: {e}")
        return False

    # ─── Funding rates ───────────────────────────────────────────────────

    async def _update_funding_rates(self) -> None:
        """Обновить ставки финансирования со всех бирж"""
        try:
            tasks = [
                self.okx_client.get_funding_rates_all(),
                self.htx_client.get_funding_rates(),
            ]
            if self.bybit_client:
                tasks.append(self.bybit_client.get_funding_rates())

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # OKX
            okx_result = results[0]
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for ticker in okx_result.get("data", []):
                    inst_id = ticker.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        try:
                            rate = float(ticker.get("fundingRate", 0) or 0)
                            self._funding_rates.setdefault(symbol, {})["okx"] = rate
                        except (ValueError, TypeError):
                            pass

            # HTX
            htx_result = results[1]
            if not isinstance(htx_result, Exception):
                data = htx_result.get("data", [])
                if isinstance(data, list):
                    for item in data:
                        contract_code = item.get("contract_code", "")
                        if contract_code.endswith("-USDT"):
                            symbol = contract_code.replace("-", "")
                            try:
                                rate = float(item.get("funding_rate", 0) or 0)
                                self._funding_rates.setdefault(symbol, {})["htx"] = rate
                            except (ValueError, TypeError):
                                pass

            # Bybit
            if self.bybit_client and len(results) > 2:
                bybit_result = results[2]
                if not isinstance(bybit_result, Exception) and bybit_result.get("retCode") == 0:
                    for ticker in bybit_result.get("result", {}).get("list", []):
                        symbol = ticker.get("symbol", "")
                        if symbol.endswith("USDT"):
                            try:
                                rate = float(ticker.get("fundingRate", 0) or 0)
                                self._funding_rates.setdefault(symbol, {})["bybit"] = rate
                            except (ValueError, TypeError):
                                pass

            self._last_funding_update = time.time()
            logger.debug(f"Funding rates updated: {len(self._funding_rates)} pairs")
        except Exception as e:
            logger.error(f"Funding rate update error: {e}")

    def _estimate_funding_cost(self, symbol: str, duration_hours: float, size_usd: float) -> float:
        """Оценить стоимость funding за время удержания"""
        rates = self._funding_rates.get(symbol, {})
        if not rates:
            return 0
        total_rate = abs(rates.get("okx", 0)) + abs(rates.get("htx", 0)) + abs(rates.get("bybit", 0))
        return total_rate * (duration_hours / 8.0) * size_usd

    # ─── Spot-futures basis ──────────────────────────────────────────────

    async def _update_basis_spreads(self) -> None:
        """Мониторинг спреда spot vs futures"""
        try:
            tasks = [
                self.okx_client.get_spot_tickers(),
                self.htx_client.get_spot_tickers(),
            ]
            if self.bybit_client:
                tasks.append(self.bybit_client.get_spot_tickers())

            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._basis_spreads = []

            # OKX
            okx_spot = results[0]
            if not isinstance(okx_spot, Exception) and okx_spot.get("code") == "0":
                for ticker in okx_spot.get("data", []):
                    inst_id = ticker.get("instId", "")
                    if "-USDT" in inst_id and "SWAP" not in inst_id:
                        symbol = inst_id.replace("-USDT", "").replace("-", "") + "USDT"
                        try:
                            spot = float(ticker.get("last", 0))
                            futures = self.exchange_prices.get("okx", {}).get(symbol)
                            if spot > 0 and futures:
                                mid = (futures["bid"] + futures["ask"]) / 2
                                basis = (mid - spot) / spot * 100
                                if abs(basis) >= self.config.min_basis:
                                    self._basis_spreads.append(BasisSpread(
                                        symbol=symbol, spot_price=spot,
                                        futures_price=mid, basis_pct=basis, exchange="okx"
                                    ))
                        except (ValueError, TypeError):
                            pass

            # HTX
            htx_spot = results[1]
            if not isinstance(htx_spot, Exception):
                data = htx_spot.get("data", [])
                if isinstance(data, list):
                    for ticker in data:
                        sym = ticker.get("symbol", "")
                        if sym.endswith("usdt"):
                            symbol = sym.upper()
                            try:
                                spot = float(ticker.get("close", 0))
                                futures = self.exchange_prices.get("htx", {}).get(symbol)
                                if spot > 0 and futures:
                                    mid = (futures["bid"] + futures["ask"]) / 2
                                    basis = (mid - spot) / spot * 100
                                    if abs(basis) >= self.config.min_basis:
                                        self._basis_spreads.append(BasisSpread(
                                            symbol=symbol, spot_price=spot,
                                            futures_price=mid, basis_pct=basis, exchange="htx"
                                        ))
                            except (ValueError, TypeError):
                                pass

            # Bybit
            if self.bybit_client and len(results) > 2:
                bybit_spot = results[2]
                if not isinstance(bybit_spot, Exception) and bybit_spot.get("retCode") == 0:
                    for ticker in bybit_spot.get("result", {}).get("list", []):
                        symbol = ticker.get("symbol", "")
                        if symbol.endswith("USDT"):
                            try:
                                spot = float(ticker.get("lastPrice", 0))
                                futures = self.exchange_prices.get("bybit", {}).get(symbol)
                                if spot > 0 and futures:
                                    mid = (futures["bid"] + futures["ask"]) / 2
                                    basis = (mid - spot) / spot * 100
                                    if abs(basis) >= self.config.min_basis:
                                        self._basis_spreads.append(BasisSpread(
                                            symbol=symbol, spot_price=spot,
                                            futures_price=mid, basis_pct=basis, exchange="bybit"
                                        ))
                            except (ValueError, TypeError):
                                pass

            self._basis_spreads.sort(key=lambda x: abs(x.basis_pct), reverse=True)
        except Exception as e:
            logger.error(f"Basis update error: {e}")

    # ─── Balance alerts ──────────────────────────────────────────────────

    async def _check_balance_alerts(self) -> None:
        """Уведомления о состоянии баланса"""
        now = time.time()
        bal = self.state.total_balance

        if self._initial_balance == 0 and bal > 0:
            self._initial_balance = bal
            self._prev_balance = bal

        if bal <= 0:
            return

        # Снэпшот в БД
        if self._trade_db_initialized and now - self._last_balance_notification > 300:
            try:
                from arbitrage.core.trade_history import save_balance_snapshot
                await save_balance_snapshot(self.state.okx_balance, self.state.htx_balance, bal)
            except Exception:
                pass

        # Критическое падение
        if self._prev_balance > 0:
            drop = (self._prev_balance - bal) / self._prev_balance * 100
            if drop >= self._balance_alert_threshold_pct:
                await self.notifications.send(
                    f"🚨 <b>Падение баланса!</b>\n\n"
                    f"Было: ${self._prev_balance:.2f}\nСтало: ${bal:.2f}\n"
                    f"Падение: {drop:.1f}%"
                )
                self._prev_balance = bal
                return

        # Периодический отчёт
        if now - self._last_balance_notification >= self._balance_notification_interval:
            change = bal - self._initial_balance
            sign = "+" if change >= 0 else ""
            pct = (change / self._initial_balance * 100) if self._initial_balance > 0 else 0

            balance_lines = []
            for ex in self.exchanges:
                ex_bal = getattr(self.state, f"{ex}_balance", 0)
                balance_lines.append(f"{ex.upper()}: ${ex_bal:.2f}")

            await self.notifications.send(
                f"💰 <b>Баланс</b>\n\n"
                f"{chr(10).join(balance_lines)}\n"
                f"Итого: <b>${bal:.2f}</b>\n\n"
                f"Изменение: {sign}${change:.2f} ({sign}{pct:.1f}%)\n"
                f"Сделок: {self.state.total_trades} | PnL: ${self.state.total_pnl:.4f}"
            )
            self._last_balance_notification = now
            self._prev_balance = bal

    # ─── Auto-blacklist ──────────────────────────────────────────────────

    async def _load_blacklist(self) -> None:
        if not self._trade_db_initialized:
            return
        try:
            from arbitrage.core.trade_history import get_blacklisted_pairs
            self._blacklisted_pairs = await get_blacklisted_pairs()
            if self._blacklisted_pairs:
                logger.info(f"Blacklisted: {self._blacklisted_pairs}")
        except Exception as e:
            logger.error(f"Blacklist load error: {e}")

    async def _periodic_blacklist_check(self) -> None:
        now = time.time()
        if now - self._last_blacklist_check < 600:
            return
        self._last_blacklist_check = now
        if not self._trade_db_initialized:
            return
        try:
            from arbitrage.core.trade_history import check_auto_blacklist
            newly = await check_auto_blacklist()
            if newly:
                self._blacklisted_pairs.update(newly)
                await self.notifications.send(
                    f"🚫 <b>Auto-blacklist</b>\n\n"
                    f"Заблокированы: {', '.join(newly)}"
                )
        except Exception as e:
            logger.error(f"Blacklist check error: {e}")

    # ─── Dynamic position sizing ─────────────────────────────────────────

    def _calculate_dynamic_position_pct(self, symbol: str, spread: float) -> float:
        base_pct = self.runtime_settings.get("max_position_pct", self.config.max_position_pct)

        if self._consecutive_losses >= 3:
            base_pct *= 0.5
        elif self._consecutive_losses >= 2:
            base_pct *= 0.75

        entry_thr = self._get_entry_threshold(symbol)
        if entry_thr > 0 and spread / entry_thr >= 2.0:
            base_pct = min(base_pct * 1.25, 0.50)
        elif entry_thr > 0 and spread / entry_thr >= 1.5:
            base_pct = min(base_pct * 1.1, 0.40)

        return base_pct

    # ─── Liquidation protection ──────────────────────────────────────────

    def _check_liquidation_risk(self, size_usd: float) -> Tuple[bool, str]:
        leverage = int(self.runtime_settings.get("leverage", self.config.leverage))
        if leverage <= 1:
            return True, "OK"

        liq_distance = (100 / leverage) - 0.5
        if liq_distance < 4.0:
            return False, f"Liquidation risk: leverage={leverage}x, liq at {liq_distance:.1f}%"

        margin_needed = (size_usd / leverage) * 2
        available = self.state.total_balance
        free_margin_pct = (available - margin_needed) / available * 100 if available > 0 else 0
        if free_margin_pct < 30:
            return False, f"Free margin {free_margin_pct:.0f}% < 30%"

        return True, "OK"

    # ─── Exchange abstraction ────────────────────────────────────────────

    def get_supported_exchanges(self) -> List[str]:
        return list(self.exchanges.keys())

    def get_exchange_client(self, exchange: str):
        client = self.exchanges.get(exchange)
        if not client:
            raise ValueError(f"Unknown exchange: {exchange}")
        return client

    # ─── Account mode check ──────────────────────────────────────────────

    async def _check_okx_account_mode(self) -> bool:
        try:
            result = await self.okx_client._request("GET", "/api/v5/account/config")
            if result.get("code") != "0" or not result.get("data"):
                return False
            acct_data = result["data"][0]
            acct_lv = acct_data.get("acctLv", "1")
            perm = acct_data.get("perm", "")
            if acct_lv == "1":
                logger.error("OKX SIMPLE mode — futures disabled")
                return False
            if "trade" not in perm.lower():
                logger.error(f"OKX no trade perm: {perm}")
                return False
            logger.info(f"OKX account OK: acctLv={acct_lv}, perm={perm}")
            return True
        except Exception as e:
            logger.error(f"OKX account check failed: {e}")
            return False

    # ─── Filter pairs ─────────────────────────────────────────────────

    async def _filter_pairs(self, pairs: Set[str]) -> Set[str]:
        if self.config.mock_mode:
            return POPULAR_PAIRS & pairs

        filtered = set()
        try:
            # Fetch volumes from all exchanges
            tasks = [
                self.okx_client.get_tickers(inst_type="SWAP"),
                self.htx_client.get_tickers(),
            ]
            if self.bybit_client:
                tasks.append(self.bybit_client.get_tickers())

            results = await asyncio.gather(*tasks, return_exceptions=True)

            volumes: Dict[str, Dict[str, float]] = {}  # {exchange: {symbol: volume}}

            # OKX
            okx_result = results[0]
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for ticker in okx_result.get("data", []):
                    inst_id = ticker.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        try:
                            volumes.setdefault("okx", {})[symbol] = float(ticker.get("vol24h", 0)) * float(ticker.get("last", 1))
                        except (ValueError, TypeError):
                            pass

            # HTX
            htx_result = results[1]
            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for ticker in htx_result.get("ticks", []):
                    cc = ticker.get("contract_code", "")
                    if cc.endswith("-USDT"):
                        symbol = cc.replace("-", "")
                        try:
                            volumes.setdefault("htx", {})[symbol] = float(ticker.get("amount", 0)) * float(ticker.get("close", 0))
                        except (ValueError, TypeError):
                            pass

            # Bybit
            if self.bybit_client and len(results) > 2:
                bybit_result = results[2]
                if not isinstance(bybit_result, Exception) and bybit_result.get("retCode") == 0:
                    for ticker in bybit_result.get("result", {}).get("list", []):
                        symbol = ticker.get("symbol", "")
                        if symbol.endswith("USDT"):
                            try:
                                volumes.setdefault("bybit", {})[symbol] = float(ticker.get("turnover24h", 0))
                            except (ValueError, TypeError):
                                pass

            # A pair is filtered in if it has sufficient volume on at least 2 exchanges
            for pair in pairs:
                if pair in self._blacklisted_pairs:
                    continue
                exchanges_with_volume = 0
                for ex_vols in volumes.values():
                    if ex_vols.get(pair, 0) >= self._min_volume_usd:
                        exchanges_with_volume += 1
                if exchanges_with_volume >= 2:
                    filtered.add(pair)

            filtered |= ((POPULAR_PAIRS & pairs) - self._blacklisted_pairs)
            logger.info(f"Filtered: {len(filtered)} pairs ({len(self._blacklisted_pairs)} blacklisted)")
        except Exception as e:
            logger.error(f"Filter error: {e}")
            filtered = (POPULAR_PAIRS & pairs) - self._blacklisted_pairs
        return filtered

    # ─── Обновление цен ──────────────────────────────────────────────────

    async def update_prices(self) -> int:
        """Fetch prices from all exchanges in parallel"""
        try:
            tasks = [
                self._fetch_exchange_prices("okx"),
                self._fetch_exchange_prices("htx"),
            ]
            if self.bybit_client:
                tasks.append(self._fetch_exchange_prices("bybit"))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            total = sum(r for r in results if isinstance(r, int))
            return total
        except Exception as e:
            logger.error(f"Price update error: {e}")
            return 0

    async def _fetch_exchange_prices(self, exchange: str) -> int:
        """Fetch prices for a single exchange"""
        updated = 0
        try:
            if exchange == "okx":
                result = await self.okx_client.get_tickers(inst_type="SWAP")
                if result.get("code") == "0":
                    for ticker in result.get("data", []):
                        inst_id = ticker.get("instId", "")
                        if "-USDT-SWAP" in inst_id:
                            symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                            if symbol in self.monitored_pairs:
                                try:
                                    bid = float(ticker.get("bidPx") or 0)
                                    ask = float(ticker.get("askPx") or 0)
                                    if bid > 0 and ask > 0:
                                        self.exchange_prices.setdefault("okx", {})[symbol] = {"bid": bid, "ask": ask}
                                        updated += 1
                                except (ValueError, TypeError):
                                    pass

            elif exchange == "htx":
                result = await self.htx_client.get_tickers()
                if result.get("status") == "ok":
                    for ticker in result.get("ticks", []):
                        cc = ticker.get("contract_code", "")
                        if cc.endswith("-USDT"):
                            symbol = cc.replace("-", "")
                            if symbol in self.monitored_pairs:
                                try:
                                    bid_data = ticker.get("bid", [0, 0])
                                    ask_data = ticker.get("ask", [0, 0])
                                    bid = float(bid_data[0]) if bid_data else 0
                                    ask = float(ask_data[0]) if ask_data else 0
                                    if bid > 0 and ask > 0:
                                        self.exchange_prices.setdefault("htx", {})[symbol] = {"bid": bid, "ask": ask}
                                        updated += 1
                                except (ValueError, TypeError, IndexError):
                                    pass

            elif exchange == "bybit":
                if not self.bybit_client:
                    return 0
                result = await self.bybit_client.get_tickers()
                if result.get("retCode") == 0:
                    for ticker in result.get("result", {}).get("list", []):
                        symbol = ticker.get("symbol", "")
                        if symbol in self.monitored_pairs:
                            try:
                                bid = float(ticker.get("bid1Price") or 0)
                                ask = float(ticker.get("ask1Price") or 0)
                                if bid > 0 and ask > 0:
                                    self.exchange_prices.setdefault("bybit", {})[symbol] = {"bid": bid, "ask": ask}
                                    updated += 1
                            except (ValueError, TypeError):
                                pass
        except Exception as e:
            logger.error(f"{exchange.upper()} price fetch error: {e}")
        return updated

    def calculate_spreads(self) -> List[PairSpread]:
        """Calculate spreads across ALL exchange pairs"""
        spreads = []
        now = time.time()
        cutoff = now - self._spread_history_window
        exchange_names = list(self.exchanges.keys())

        for symbol in self.monitored_pairs:
            best_spread = 0
            best_long_ex = ""
            best_short_ex = ""
            best_long_price = 0
            best_short_price = 0

            # Check all exchange pairs
            for i, ex1 in enumerate(exchange_names):
                p1 = self.exchange_prices.get(ex1, {}).get(symbol)
                if not p1:
                    continue
                for ex2 in exchange_names[i+1:]:
                    p2 = self.exchange_prices.get(ex2, {}).get(symbol)
                    if not p2:
                        continue

                    # Direction 1: long ex1, short ex2
                    spread1 = calculate_spread(p2["bid"], p1["ask"])
                    # Direction 2: long ex2, short ex1
                    spread2 = calculate_spread(p1["bid"], p2["ask"])

                    if spread1 > best_spread:
                        best_spread = spread1
                        best_long_ex = ex1
                        best_short_ex = ex2
                        best_long_price = p1["ask"]
                        best_short_price = p2["bid"]

                    if spread2 > best_spread:
                        best_spread = spread2
                        best_long_ex = ex2
                        best_short_ex = ex1
                        best_long_price = p2["ask"]
                        best_short_price = p1["bid"]

            # Update spread history with best spread for this symbol
            if best_spread > 0:
                if symbol not in self._spread_history:
                    self._spread_history[symbol] = []
                self._spread_history[symbol].append((best_spread, now))
                self._spread_history[symbol] = [(s, t) for s, t in self._spread_history[symbol] if t > cutoff]
                self._update_pair_stats(symbol)

            if best_spread >= self.min_spread and best_long_ex:
                spreads.append(PairSpread(
                    symbol=symbol, spread=best_spread,
                    long_exchange=best_long_ex, short_exchange=best_short_ex,
                    long_price=best_long_price, short_price=best_short_price
                ))

        spreads.sort(key=lambda x: x.spread, reverse=True)
        return spreads

    def _update_pair_stats(self, symbol: str) -> Optional[Dict]:
        hist = self._spread_history.get(symbol, [])
        if len(hist) < 20:
            return None
        vals = [s for s, _ in hist]
        mean = sum(vals) / len(vals)
        std = (sum((s - mean) ** 2 for s in vals) / len(vals)) ** 0.5
        stats = {"mean": mean, "std": std, "min": min(vals), "max": max(vals), "samples": len(vals)}
        self._pair_stats[symbol] = stats
        return stats

    def _get_entry_threshold(self, symbol: str) -> float:
        base = self.runtime_settings.get("entry_threshold", self.config.entry_threshold)
        stats = self._pair_stats.get(symbol)
        if not stats or stats["samples"] < 20:
            return base
        # Adaptive: mean + 1.2*std (was 2*std — too conservative, caused missed trades)
        adaptive = stats["mean"] + 1.2 * stats["std"]
        # Fee floor: ensure profit after ~0.18% fees per side
        min_entry = stats["mean"] + 0.20
        # Cap adaptive at base * 2.5 to prevent unreachable thresholds
        cap = base * 2.5
        return min(max(base, adaptive, min_entry), cap)

    def _get_exit_threshold(self, symbol: str) -> float:
        base = self.runtime_settings.get("exit_threshold", self.config.exit_threshold)
        stats = self._pair_stats.get(symbol)
        if not stats or stats["samples"] < 20:
            return base
        # Cap exit threshold too — don't let it grow beyond base * 2
        return min(max(base, stats["mean"] * 0.8), base * 2)

    # ─── Opportunity tracking ────────────────────────────────────────────

    def _track_opportunities(self, current_spreads: List[PairSpread]) -> None:
        current_time = time.time()
        current_opps = {s.symbol: s for s in current_spreads}
        grace = self.update_interval * 3

        for symbol in list(self.active_opportunities.keys()):
            if symbol not in current_opps:
                old_spread, start_time, _ = self.active_opportunities[symbol]
                last_seen = getattr(old_spread, '_last_seen', start_time)
                if current_time - last_seen > grace:
                    del self.active_opportunities[symbol]

        for symbol, spread in current_opps.items():
            spread._last_seen = current_time
            if symbol not in self.active_opportunities:
                self.active_opportunities[symbol] = (spread, current_time, spread.spread)
            else:
                _, start_time, old_peak = self.active_opportunities[symbol]
                self.active_opportunities[symbol] = (spread, start_time, max(old_peak, spread.spread))

    # ─── Балансы ─────────────────────────────────────────────────────────

    async def _update_balances(self) -> None:
        """Update balances from all exchanges"""
        try:
            tasks = [
                self.okx_client.get_balance(),
                self.htx_client.get_balance(),
            ]
            if self.bybit_client:
                tasks.append(self.bybit_client.get_balance())

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # OKX
            okx_data = results[0]
            if not isinstance(okx_data, Exception):
                if okx_data.get("code") == "0" and okx_data.get("data"):
                    for detail in okx_data["data"][0].get("details", []):
                        if detail.get("ccy") == "USDT":
                            self.state.update_balance("okx", float(detail.get("availBal", 0)))
                            break

            # HTX
            htx_data = results[1]
            if not isinstance(htx_data, Exception):
                if htx_data.get("code") == 200 and htx_data.get("data"):
                    data = htx_data["data"]
                    if isinstance(data, list):
                        for item in data:
                            if item.get("margin_asset") == "USDT":
                                self.state.update_balance("htx", float(item.get("withdraw_available", 0)))
                                break

            # Bybit
            if self.bybit_client and len(results) > 2:
                bybit_data = results[2]
                if not isinstance(bybit_data, Exception):
                    if bybit_data.get("retCode") == 0 and bybit_data.get("result"):
                        for coin in bybit_data["result"].get("list", [{}])[0].get("coin", []):
                            if coin.get("coin") == "USDT":
                                # Try multiple balance fields (Bybit may return "" for some)
                                for field in ["availableToWithdraw", "walletBalance", "equity"]:
                                    val = coin.get(field, "")
                                    if val and val != "0":
                                        try:
                                            self.state.update_balance("bybit", float(val))
                                            break
                                        except (ValueError, TypeError):
                                            continue
                                break

            balance_parts = []
            for ex in self.exchanges:
                ex_bal = getattr(self.state, f"{ex}_balance", 0)
                balance_parts.append(f"{ex.upper()}=${ex_bal:.2f}")
            logger.info(f"Balances: {', '.join(balance_parts)}")
        except Exception as e:
            logger.error(f"Balance update error: {e}", exc_info=True)

    def _calculate_contracts(self, exchange: str, symbol: str, usd_per_side: float, price: float) -> int:
        """Calculate number of contracts for a given exchange"""
        ct = self.contract_sizes.get(exchange, {}).get(symbol, 0)
        if ct <= 0 or price <= 0:
            return 0
        coins = usd_per_side / price
        return int(max(0, math.floor(coins / ct)))

    @staticmethod
    def _check_order_result(exchange: str, result) -> bool:
        if isinstance(result, Exception):
            return False
        if exchange == "okx":
            return result.get("code") == "0" and bool(result.get("data"))
        elif exchange == "htx":
            return result.get("status") == "ok" and bool(result.get("data"))
        elif exchange == "bybit":
            return result.get("retCode") == 0 and bool(result.get("result"))
        return False

    @staticmethod
    def _is_connectivity_error(exchange: str, result) -> bool:
        """Check if result indicates connectivity error (not business error)"""
        if isinstance(result, Exception) or not isinstance(result, dict):
            return True
        if exchange == "htx":
            if result.get("status") == "error" and result.get("err_code"):
                return False
            if result.get("status") == "error" and result.get("err-msg"):
                return True
        elif exchange == "bybit":
            if result.get("retCode", 0) != 0:
                return result.get("retCode", 0) in (-1, 10000, 10001)
        return False

    # ─── Order placement helpers ─────────────────────────────────────────

    async def _place_exchange_order(
        self, exchange: str, symbol: str, side: str,
        contracts: int, order_type: str, price: float = 0,
        offset: str = "open", leverage: int = 1
    ) -> Dict:
        """Place order on any exchange with unified interface"""
        client = self.exchanges[exchange]
        if exchange == "okx":
            return await client.place_order(symbol, side, contracts, order_type, price,
                                             "ioc" if order_type == "limit" else "")
        elif exchange == "htx":
            return await client.place_order(symbol, side, contracts, order_type,
                                             offset=offset, lever_rate=leverage)
        elif exchange == "bybit":
            return await client.place_order(symbol, side, contracts, order_type,
                                             price=price, offset=offset, lever_rate=leverage)
        return {"retCode": -1, "retMsg": f"Unknown exchange: {exchange}"}

    async def _set_exchange_leverage(self, exchange: str, symbol: str, leverage: int) -> None:
        """Set leverage on exchange, ignore non-critical errors"""
        try:
            client = self.exchanges[exchange]
            if exchange == "okx":
                await client.set_leverage(symbol, leverage)
            elif exchange == "htx":
                r = await client.set_leverage(symbol, leverage)
                if isinstance(r, dict) and r.get("status") == "error":
                    err = r.get("err-msg", "")
                    if "already" not in err.lower() and "same" not in err.lower():
                        logger.warning(f"HTX leverage: {err}")
            elif exchange == "bybit":
                r = await client.set_leverage(symbol, leverage)
                if isinstance(r, dict) and r.get("retCode") != 0:
                    msg = r.get("retMsg", "")
                    if "not modified" not in msg.lower() and "pm mode" not in msg.lower():
                        logger.warning(f"Bybit leverage: {msg}")
        except Exception:
            pass

    async def _get_fill_price(self, exchange: str, symbol: str, order_result: Dict) -> float:
        """Get actual fill price from order result"""
        try:
            if exchange == "okx":
                if order_result.get("data"):
                    ord_id = order_result["data"][0].get("ordId", "")
                    if ord_id:
                        await asyncio.sleep(0.3)
                        info = await self.okx_client.get_order(symbol, ord_id)
                        if info.get("code") == "0" and info.get("data"):
                            return float(info["data"][0].get("avgPx", 0) or 0)
            elif exchange == "htx":
                # HTX fill price comes from position check
                result = await self.htx_client.get_cross_position(symbol)
                if result.get("status") == "ok":
                    for pos in result.get("data", []):
                        if float(pos.get("volume", 0)) > 0:
                            return float(pos.get("cost_open", 0) or 0)
            elif exchange == "bybit":
                if order_result.get("result"):
                    ord_id = order_result["result"].get("orderId", "")
                    if ord_id:
                        await asyncio.sleep(0.3)
                        info = await self.bybit_client.get_order(symbol, ord_id)
                        if info.get("retCode") == 0 and info.get("result", {}).get("list"):
                            return float(info["result"]["list"][0].get("avgPrice", 0) or 0)
        except Exception as e:
            logger.error(f"Get fill price error ({exchange}): {e}")
        return 0

    async def _verify_position(self, exchange: str, symbol: str) -> Tuple[float, float]:
        """Verify position exists, return (actual_contracts, fill_price)"""
        try:
            if exchange == "okx":
                result = await self.okx_client.get_positions()
                if result.get("code") == "0":
                    for pos in result.get("data", []):
                        inst_id = pos.get("instId", "")
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        if sym == symbol and float(pos.get("pos", 0)) != 0:
                            return abs(float(pos["pos"])), float(pos.get("avgPx", 0) or 0)
            elif exchange == "htx":
                result = await self.htx_client.get_cross_position(symbol)
                if result.get("status") == "ok":
                    for pos in result.get("data", []):
                        vol = float(pos.get("volume", 0))
                        if vol > 0:
                            return vol, float(pos.get("cost_open", 0) or 0)
            elif exchange == "bybit":
                if self.bybit_client:
                    result = await self.bybit_client.get_cross_position(symbol)
                    if result.get("retCode") == 0:
                        for pos in result.get("result", {}).get("list", []):
                            size = float(pos.get("size", 0))
                            if size > 0:
                                return size, float(pos.get("avgPrice", 0) or 0)
        except Exception as e:
            logger.error(f"Verify position error ({exchange}): {e}")
        return 0, 0

    # ─── Entry ───────────────────────────────────────────────────────────

    async def _scan_for_entry(self, spreads: List[PairSpread]) -> None:
        if self.active_trade:
            return
        for spread in spreads:
            symbol = spread.symbol
            if symbol in self._blacklisted_pairs:
                continue
            entry_thr = self._get_entry_threshold(symbol)
            if spread.spread < entry_thr:
                continue
            if symbol in self.active_opportunities:
                _, start_time, peak = self.active_opportunities[symbol]
                if time.time() - start_time < self.config.min_opportunity_lifetime:
                    continue
                if peak - spread.spread < self._min_pullback_from_peak:
                    continue
                logger.info(f"Entry signal {symbol}: {spread.spread:.3f}% (thr={entry_thr:.3f}%) "
                           f"L:{spread.long_exchange} S:{spread.short_exchange}")
                if await self._execute_entry(spread):
                    return

    async def _pre_check_exchange(self, exchange: str) -> bool:
        """Pre-check exchange connectivity"""
        try:
            client = self.exchanges[exchange]
            if exchange == "okx":
                return True  # OKX is always "reliable"
            elif exchange == "htx":
                result = await client.get_balance()
                return isinstance(result, dict) and (result.get("code") == 200 or result.get("status") == "ok")
            elif exchange == "bybit":
                result = await client.get_balance()
                return isinstance(result, dict) and result.get("retCode") == 0
        except Exception:
            return False

    def _check_circuit_breaker(self, exchange: str) -> bool:
        """Returns True if exchange is OK (not tripped)"""
        cb = self._circuit_breakers.get(exchange)
        if not cb:
            return True
        return time.time() >= cb["breaker_until"]

    def _record_exchange_failure(self, exchange: str) -> None:
        cb = self._circuit_breakers.get(exchange)
        if not cb:
            return
        cb["consecutive_failures"] += 1
        if cb["consecutive_failures"] >= cb["max_failures"]:
            cb["breaker_until"] = time.time() + cb["breaker_seconds"]

    def _reset_exchange_failures(self, exchange: str) -> None:
        cb = self._circuit_breakers.get(exchange)
        if cb:
            cb["consecutive_failures"] = 0

    async def _execute_entry(self, spread: PairSpread) -> bool:
        symbol = spread.symbol
        long_ex = spread.long_exchange
        short_ex = spread.short_exchange

        # Determine which exchange is the "risky" leg (non-OKX, first to execute)
        # OKX is always the second (safer) leg since IOC ensures instant fill/cancel
        if long_ex == "okx":
            first_leg_ex = short_ex
            first_leg_side = "sell"
            second_leg_side = "buy"
        elif short_ex == "okx":
            first_leg_ex = long_ex
            first_leg_side = "buy"
            second_leg_side = "sell"
        else:
            # Neither is OKX — use the first alphabetically as first leg
            first_leg_ex = min(long_ex, short_ex)
            first_leg_side = "buy" if first_leg_ex == long_ex else "sell"
            second_leg_side = "sell" if first_leg_side == "buy" else "buy"

        second_leg_ex = long_ex if first_leg_ex == short_ex else short_ex

        # Circuit breaker check
        if not self._check_circuit_breaker(first_leg_ex):
            return False
        if not self._check_circuit_breaker(second_leg_ex):
            return False

        # Pre-check first leg exchange
        if first_leg_ex != "okx":
            if not await self._pre_check_exchange(first_leg_ex):
                self._record_exchange_failure(first_leg_ex)
                cb = self._circuit_breakers.get(first_leg_ex, {})
                if cb.get("consecutive_failures", 0) >= cb.get("max_failures", 2):
                    await self.notifications.notify_error(
                        f"{first_leg_ex.upper()} CIRCUIT BREAKER", "Trading paused")
                return False

        await self._update_balances()
        if self.state.total_balance < 2:
            return False

        position_pct = self._calculate_dynamic_position_pct(symbol, spread.spread)

        # Calculate per-side USD based on balances of the two exchanges involved
        long_balance = getattr(self.state, f"{long_ex}_balance", 0)
        short_balance = getattr(self.state, f"{short_ex}_balance", 0)
        long_side = max(4.0, long_balance * position_pct)
        short_side = max(4.0, short_balance * position_pct)

        if long_side > long_balance or short_side > short_balance:
            return False

        per_side_usd = min(long_side, short_side)
        avg_price = (spread.long_price + spread.short_price) / 2

        liq_ok, liq_msg = self._check_liquidation_risk(per_side_usd)
        if not liq_ok:
            logger.warning(f"Liquidation risk: {liq_msg}")
            return False

        first_contracts = self._calculate_contracts(first_leg_ex, symbol, per_side_usd, avg_price)
        second_contracts = self._calculate_contracts(second_leg_ex, symbol, per_side_usd, avg_price)
        if first_contracts < 1 or second_contracts < 1:
            return False

        leverage = int(self.runtime_settings.get("leverage", self.config.leverage))

        # Set leverage on both exchanges
        await asyncio.gather(
            self._set_exchange_leverage(first_leg_ex, symbol, leverage),
            self._set_exchange_leverage(second_leg_ex, symbol, leverage),
        )

        try:
            # Step 1: First leg order (risky leg)
            first_order_type = "optimal_5" if first_leg_ex == "htx" else "market"
            if first_leg_ex == "bybit":
                first_order_type = "market"

            first_result = await self._place_exchange_order(
                first_leg_ex, symbol, first_leg_side, first_contracts,
                first_order_type, offset="open", leverage=leverage
            )

            if not self._check_order_result(first_leg_ex, first_result):
                if self._is_connectivity_error(first_leg_ex, first_result):
                    self._record_exchange_failure(first_leg_ex)
                    cb = self._circuit_breakers.get(first_leg_ex, {})
                    if cb.get("consecutive_failures", 0) >= cb.get("max_failures", 2):
                        await self.notifications.notify_error(
                            f"{first_leg_ex.upper()} CIRCUIT BREAKER", "Connectivity failed")
                else:
                    self._reset_exchange_failures(first_leg_ex)
                return False

            self._reset_exchange_failures(first_leg_ex)

            # Verify first leg position
            await asyncio.sleep(0.5)
            first_actual, first_fill_price = await self._verify_position(first_leg_ex, symbol)
            if first_actual == 0:
                logger.warning(f"{first_leg_ex.upper()} no position for {symbol}")
                return False
            first_contracts = int(first_actual)

            # Step 2: Second leg order (OKX with IOC, or market)
            slippage = 0.0015
            if second_leg_ex == "okx":
                if second_leg_side == "buy":
                    px = round(spread.long_price * (1 + slippage), 10)
                else:
                    px = round(spread.short_price * (1 - slippage), 10)
                second_result = await self._place_exchange_order(
                    second_leg_ex, symbol, second_leg_side, second_contracts,
                    "limit", price=px, offset="open", leverage=leverage
                )
            else:
                order_type = "optimal_5" if second_leg_ex == "htx" else "market"
                second_result = await self._place_exchange_order(
                    second_leg_ex, symbol, second_leg_side, second_contracts,
                    order_type, offset="open", leverage=leverage
                )

            second_ok = self._check_order_result(second_leg_ex, second_result)
            second_fill_price = 0

            if second_ok and second_leg_ex == "okx":
                # Verify OKX fill
                if isinstance(second_result, dict) and second_result.get("data"):
                    ord_id = second_result["data"][0].get("ordId", "")
                    if ord_id:
                        await asyncio.sleep(0.3)
                        info = await self.okx_client.get_order(symbol, ord_id)
                        if info.get("code") == "0" and info.get("data"):
                            fill_sz = float(info["data"][0].get("fillSz", 0))
                            state = info["data"][0].get("state", "")
                            second_fill_price = float(info["data"][0].get("avgPx", 0) or 0)
                            if fill_sz == 0 or state == "canceled":
                                second_ok = False
                            else:
                                second_contracts = int(fill_sz)
            elif second_ok:
                await asyncio.sleep(0.5)
                second_actual, second_fill_price = await self._verify_position(second_leg_ex, symbol)
                if second_actual == 0:
                    second_ok = False
                else:
                    second_contracts = int(second_actual)

            if second_ok:
                # Determine long/short contracts
                if first_leg_ex == long_ex:
                    long_contracts = first_contracts
                    short_contracts = second_contracts
                    long_fill = first_fill_price
                    short_fill = second_fill_price
                else:
                    long_contracts = second_contracts
                    short_contracts = first_contracts
                    long_fill = second_fill_price
                    short_fill = first_fill_price

                long_ct = self.contract_sizes.get(long_ex, {}).get(symbol, 1)
                short_ct = self.contract_sizes.get(short_ex, {}).get(symbol, 1)
                actual_usd = long_contracts * long_ct * avg_price + short_contracts * short_ct * avg_price

                # Balance positions (trim excess)
                long_coins = long_contracts * long_ct
                short_coins = short_contracts * short_ct
                if short_coins > long_coins * 1.5 and short_contracts > 1:
                    target = max(1, int(long_coins / short_ct))
                    excess = short_contracts - target
                    if excess > 0:
                        close_side = "sell" if short_ex == long_ex else "buy"
                        # Actually: if short_exchange, we sold, so close = buy
                        close_side = "buy"
                        try:
                            close_type = "opponent" if short_ex == "htx" else "market"
                            await self._place_exchange_order(
                                short_ex, symbol, close_side, excess,
                                close_type, offset="close", leverage=leverage
                            )
                            short_contracts = target
                        except Exception as e:
                            logger.error(f"Trim excess: {e}")

                exit_thr = self._get_exit_threshold(symbol)
                entry_thr = self._get_entry_threshold(symbol)
                slippage_pct = 0
                if long_fill > 0 and short_fill > 0:
                    expected = (spread.long_price + spread.short_price) / 2
                    actual = (long_fill + short_fill) / 2
                    slippage_pct = abs(actual - expected) / expected * 100

                trade_id = 0
                if self._trade_db_initialized:
                    try:
                        from arbitrage.core.trade_history import save_trade_open
                        trade_id = await save_trade_open(
                            symbol, long_ex, short_ex, spread.spread, actual_usd,
                            long_contracts, short_contracts, spread.long_price, spread.short_price,
                            entry_thr, exit_thr
                        )
                    except Exception as e:
                        logger.error(f"DB save error: {e}")

                self.active_trade = ActiveTrade(
                    symbol=symbol, long_exchange=long_ex, short_exchange=short_ex,
                    long_price=spread.long_price, short_price=spread.short_price,
                    entry_spread=spread.spread, size_usd=actual_usd,
                    long_contracts=long_contracts, short_contracts=short_contracts,
                    entry_time=time.time(), dynamic_exit_threshold=exit_thr, trade_id=trade_id,
                    actual_long_fill_price=long_fill,
                    actual_short_fill_price=short_fill,
                )

                logger.info(f"TRADE OPENED: {symbol} spread={spread.spread:.3f}% "
                           f"L:{long_ex} S:{short_ex} exit_thr={exit_thr:.3f}% slippage={slippage_pct:.4f}%")

                await self.notifications.notify_position_opened(
                    symbol=symbol, long_exchange=long_ex, short_exchange=short_ex,
                    size=actual_usd, long_price=spread.long_price,
                    short_price=spread.short_price, spread=spread.spread
                )
                return True
            else:
                # Second leg failed — close first leg with retries
                close_side = "sell" if first_leg_side == "buy" else "buy"
                logger.warning(f"Second leg ({second_leg_ex}) failed — hedging first leg ({first_leg_ex})")
                hedge_ok = await self._close_exchange_position(
                    first_leg_ex, symbol, close_side, first_contracts, leverage
                )
                if not hedge_ok:
                    # Hedge failed! Create active_trade so bot keeps retrying to close
                    if first_leg_ex == long_ex:
                        self.active_trade = ActiveTrade(
                            symbol=symbol, long_exchange=long_ex, short_exchange=short_ex,
                            long_price=spread.long_price, short_price=spread.short_price,
                            entry_spread=spread.spread, size_usd=per_side_usd,
                            long_contracts=first_contracts, short_contracts=0,
                            entry_time=time.time(), dynamic_exit_threshold=0,
                        )
                    else:
                        self.active_trade = ActiveTrade(
                            symbol=symbol, long_exchange=long_ex, short_exchange=short_ex,
                            long_price=spread.long_price, short_price=spread.short_price,
                            entry_spread=spread.spread, size_usd=per_side_usd,
                            long_contracts=0, short_contracts=first_contracts,
                            entry_time=time.time(), dynamic_exit_threshold=0,
                        )
                    await self.notifications.notify_error(
                        f"{first_leg_ex.upper()} HEDGE FAILED",
                        f"{symbol}: {close_side} {first_contracts}ct — auto-retry on next cycle"
                    )
                return False

        except Exception as e:
            logger.error(f"Entry error {symbol}: {e}", exc_info=True)
            return False

    # ─── Exit ────────────────────────────────────────────────────────────

    async def _check_exit(self) -> None:
        if not self.active_trade:
            return
        trade = self.active_trade

        # If previous exit partially failed, retry immediately
        if trade.long_contracts == 0 or trade.short_contracts == 0:
            logger.warning(f"Retrying partial close for {trade.symbol}")
            await self._execute_exit(trade.entry_spread, "retry_partial_close")
            return

        long_ex = trade.long_exchange
        short_ex = trade.short_exchange

        long_price = self.exchange_prices.get(long_ex, {}).get(trade.symbol)
        short_price = self.exchange_prices.get(short_ex, {}).get(trade.symbol)
        if not long_price or not short_price:
            return

        # Current spread: short_bid - long_ask (same direction as entry)
        current = calculate_spread(short_price["bid"], long_price["ask"])

        duration = time.time() - trade.entry_time
        sl = self.runtime_settings.get("stop_loss_pct", self._stop_loss_pct)
        reason = None

        if current <= trade.dynamic_exit_threshold:
            reason = "spread_converged"
        elif current >= trade.entry_spread + sl:
            reason = "stop_loss"
        elif duration >= self._max_trade_duration:
            reason = "timeout"

        if reason:
            await self._execute_exit(current, reason)

    async def _close_exchange_position(
        self, exchange: str, symbol: str, side: str, contracts: int, leverage: int
    ) -> bool:
        """Close position on exchange with 3 retries and verification"""
        close_type = "opponent" if exchange == "htx" else "market"
        for attempt in range(3):
            try:
                r = await self._place_exchange_order(
                    exchange, symbol, side, contracts,
                    close_type, offset="close", leverage=leverage
                )
                if self._check_order_result(exchange, r):
                    # Verify position is actually closed
                    await asyncio.sleep(0.5)
                    remaining, _ = await self._verify_position(exchange, symbol)
                    if remaining == 0:
                        logger.info(f"Closed {exchange.upper()} {symbol} OK")
                        return True
                    elif remaining < contracts:
                        # Partially closed — retry with remaining
                        contracts = int(remaining)
                        logger.warning(f"{exchange.upper()} {symbol}: partially closed, {remaining} remaining")
                    else:
                        logger.warning(f"{exchange.upper()} {symbol}: still open after close order")
                else:
                    logger.warning(f"{exchange.upper()} close attempt {attempt+1} failed: {r}")
            except Exception as e:
                logger.error(f"{exchange.upper()} close attempt {attempt+1} error: {e}")
            if attempt < 2:
                await asyncio.sleep(1)

        logger.error(f"FAILED to close {exchange.upper()} {symbol} after 3 attempts!")
        return False

    async def _execute_exit(self, exit_spread: float, exit_reason: str = "") -> bool:
        trade = self.active_trade
        if not trade:
            return False

        symbol = trade.symbol
        long_ex = trade.long_exchange
        short_ex = trade.short_exchange
        leverage = int(self.runtime_settings.get("leverage", self.config.leverage))

        try:
            # Close both legs with retries and verification
            # Skip sides already closed (contracts == 0 from partial close)
            long_closed = trade.long_contracts == 0  # Already closed
            short_closed = trade.short_contracts == 0  # Already closed

            if not long_closed and not short_closed:
                if long_ex == "okx":
                    # Close non-OKX first, then OKX
                    short_closed = await self._close_exchange_position(
                        short_ex, symbol, "buy", trade.short_contracts, leverage)
                    long_closed = await self._close_exchange_position(
                        long_ex, symbol, "sell", trade.long_contracts, leverage)
                elif short_ex == "okx":
                    # Close non-OKX first, then OKX
                    long_closed = await self._close_exchange_position(
                        long_ex, symbol, "sell", trade.long_contracts, leverage)
                    short_closed = await self._close_exchange_position(
                        short_ex, symbol, "buy", trade.short_contracts, leverage)
                else:
                    # Neither is OKX — close both
                    long_closed, short_closed = await asyncio.gather(
                        self._close_exchange_position(
                            long_ex, symbol, "sell", trade.long_contracts, leverage),
                        self._close_exchange_position(
                            short_ex, symbol, "buy", trade.short_contracts, leverage),
                    )
            elif not long_closed:
                long_closed = await self._close_exchange_position(
                    long_ex, symbol, "sell", trade.long_contracts, leverage)
            elif not short_closed:
                short_closed = await self._close_exchange_position(
                    short_ex, symbol, "buy", trade.short_contracts, leverage)

            # If ANY side failed — do NOT clear active_trade, keep retrying next cycle
            if not long_closed or not short_closed:
                failed_sides = []
                if not long_closed:
                    failed_sides.append(f"LONG {long_ex.upper()}")
                if not short_closed:
                    failed_sides.append(f"SHORT {short_ex.upper()}")
                await self.notifications.send(
                    f"⚠️ <b>Не удалось закрыть:</b>\n\n"
                    f"Пара: {symbol}\n"
                    f"Сторона: {', '.join(failed_sides)}\n"
                    f"Будет повторная попытка. Если не поможет — закройте вручную!"
                )
                # Mark which sides are already closed so we don't re-close them
                if long_closed:
                    trade.long_contracts = 0
                if short_closed:
                    trade.short_contracts = 0
                logger.error(f"Partial close for {symbol}: long={long_closed} short={short_closed}")
                return False

            spread_profit = trade.entry_spread - exit_spread
            fee_pct = 0.18
            pnl_usd = trade.size_usd * (spread_profit - fee_pct) / 100

            duration = time.time() - trade.entry_time
            funding_cost = self._estimate_funding_cost(symbol, duration / 3600, trade.size_usd)
            pnl_usd -= funding_cost
            fee_usd = trade.size_usd * fee_pct / 100

            slippage_pct = 0
            if trade.actual_long_fill_price > 0 and trade.actual_short_fill_price > 0:
                actual_entry = abs(trade.actual_short_fill_price - trade.actual_long_fill_price) / trade.actual_long_fill_price * 100
                slippage_pct = abs(trade.entry_spread - actual_entry)

            self.state.record_trade(success=(pnl_usd > 0), pnl=pnl_usd)

            if pnl_usd > 0:
                self._consecutive_wins += 1
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1
                self._consecutive_wins = 0

            logger.info(f"TRADE CLOSED: {symbol} PnL=${pnl_usd:.4f} "
                       f"spread={trade.entry_spread:.3f}%→{exit_spread:.3f}% "
                       f"L:{long_ex} S:{short_ex} "
                       f"funding=${funding_cost:.4f} fees=${fee_usd:.4f} reason={exit_reason}")

            if self._trade_db_initialized and trade.trade_id > 0:
                try:
                    from arbitrage.core.trade_history import save_trade_close
                    await save_trade_close(trade.trade_id, exit_spread, pnl_usd,
                        fee_usd=fee_usd, funding_cost=funding_cost,
                        slippage_pct=slippage_pct, exit_reason=exit_reason)
                except Exception as e:
                    logger.error(f"DB close error: {e}")

            await self.notifications.notify_position_closed(
                symbol=symbol, pnl=pnl_usd, long_exchange=long_ex,
                short_exchange=short_ex, size=trade.size_usd,
                duration_seconds=duration, entry_spread=trade.entry_spread, exit_spread=exit_spread
            )
            self.active_trade = None
            return True
        except Exception as e:
            logger.error(f"Exit error {symbol}: {e}", exc_info=True)
            return False

    # ─── Emergency close ─────────────────────────────────────────────────

    async def emergency_close_all(self) -> str:
        """Экстренно закрыть все позиции на всех биржах"""
        results = []

        if self.active_trade:
            trade = self.active_trade
            current = trade.entry_spread
            long_p = self.exchange_prices.get(trade.long_exchange, {}).get(trade.symbol)
            short_p = self.exchange_prices.get(trade.short_exchange, {}).get(trade.symbol)
            if long_p and short_p:
                current = calculate_spread(short_p["bid"], long_p["ask"])
            ok = await self._execute_exit(current, "emergency_close")
            results.append(f"{trade.symbol}: {'OK' if ok else 'FAILED'}")

        # Scan all exchange positions
        # OKX
        try:
            okx_pos = await self.okx_client.get_positions()
            if okx_pos.get("code") == "0":
                for pos in okx_pos.get("data", []):
                    sz = abs(float(pos.get("pos", 0)))
                    if sz > 0:
                        inst_id = pos.get("instId", "")
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        side = "sell" if float(pos.get("pos", 0)) > 0 else "buy"
                        try:
                            await self.okx_client.place_order(sym, side, sz, "market")
                            results.append(f"OKX {sym}: closed")
                        except Exception as e:
                            results.append(f"OKX {sym}: FAILED {e}")
        except Exception as e:
            results.append(f"OKX scan: {e}")

        # HTX
        try:
            htx_pos = await self.htx_client.get_positions()
            if htx_pos.get("status") == "ok":
                for pos in htx_pos.get("data", []):
                    vol = float(pos.get("volume", 0))
                    if vol > 0:
                        code = pos.get("contract_code", "")
                        sym = code.replace("-", "")
                        direction = pos.get("direction", "")
                        close_side = "sell" if direction == "buy" else "buy"
                        try:
                            await self.htx_client.place_order(sym, close_side, vol, "opponent", offset="close")
                            results.append(f"HTX {sym}: closed")
                        except Exception as e:
                            results.append(f"HTX {sym}: FAILED {e}")
        except Exception as e:
            results.append(f"HTX scan: {e}")

        # Bybit
        if self.bybit_client and not self.bybit_client.public_only:
            try:
                bybit_pos = await self.bybit_client.get_positions()
                if bybit_pos.get("retCode") == 0:
                    for pos in bybit_pos.get("result", {}).get("list", []):
                        size = float(pos.get("size", 0))
                        if size > 0:
                            sym = pos.get("symbol", "")
                            side_str = pos.get("side", "")
                            close_side = "Sell" if side_str == "Buy" else "Buy"
                            try:
                                await self.bybit_client.close_position(sym, close_side, size)
                                results.append(f"Bybit {sym}: closed")
                            except Exception as e:
                                results.append(f"Bybit {sym}: FAILED {e}")
            except Exception as e:
                results.append(f"Bybit scan: {e}")

        self.active_trade = None
        return "\n".join(results) if results else "Нет открытых позиций"

    # ─── Main loop ───────────────────────────────────────────────────────

    async def start_monitoring(self) -> None:
        exchange_names = list(self.exchanges.keys())
        logger.info(f"Starting: exchanges={','.join(e.upper() for e in exchange_names)} trading={'ON' if self.can_trade else 'OFF'}")

        if self.can_trade:
            await self._update_balances()

        cycle = 0
        while self.state.is_running:
            try:
                await self.update_prices()
                spreads = self.calculate_spreads()
                self._track_opportunities(spreads)

                if self.can_trade:
                    if self.active_trade:
                        await self._check_exit()
                    else:
                        await self._scan_for_entry(spreads)

                self.best_spreads = spreads[:10]
                cycle += 1

                if cycle % 20 == 0:
                    status = f"IN TRADE: {self.active_trade.symbol}" if self.active_trade else "scanning"
                    if spreads:
                        best = spreads[0]
                        logger.info(f"[{cycle}] Best: {best.symbol} {best.spread:.3f}% "
                                   f"L:{best.long_exchange} S:{best.short_exchange} "
                                   f"(need {self._get_entry_threshold(best.symbol):.3f}%) | "
                                   f"{status} | pairs={len(self.monitored_pairs)} | "
                                   f"balance=${self.state.total_balance:.2f} | "
                                   f"trades={self.state.total_trades} PnL=${self.state.total_pnl:.4f}")

                    if self.can_trade and cycle % 60 == 0:
                        await self._update_balances()
                        await self._check_balance_alerts()

                    if cycle % 120 == 0:
                        await self._update_funding_rates()

                    if cycle % 60 == 0:
                        await self._update_basis_spreads()

                    if self.can_trade and cycle % 120 == 0:
                        await self._periodic_blacklist_check()

                await asyncio.sleep(self.update_interval)
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                await asyncio.sleep(2)

    # ─── Public API for handlers ─────────────────────────────────────────

    async def find_best_opportunities(self, top_n: int = 10) -> List[PairSpread]:
        await self.update_prices()
        all_spreads = self.calculate_spreads()
        self.best_spreads = all_spreads[:top_n]
        self._track_opportunities(all_spreads)
        return self.best_spreads

    def get_pair_adaptive_info(self, symbol: str) -> Dict:
        stats = self._pair_stats.get(symbol, {})
        funding = self._funding_rates.get(symbol, {})
        return {
            "entry_threshold": self._get_entry_threshold(symbol),
            "exit_threshold": self._get_exit_threshold(symbol),
            "mean": stats.get("mean", 0),
            "std": stats.get("std", 0),
            "samples": stats.get("samples", 0),
            "funding_okx": funding.get("okx", 0),
            "funding_htx": funding.get("htx", 0),
            "funding_bybit": funding.get("bybit", 0),
            "blacklisted": symbol in self._blacklisted_pairs,
        }

    def update_runtime_setting(self, key: str, value: float) -> bool:
        if key in self.runtime_settings:
            old = self.runtime_settings[key]
            self.runtime_settings[key] = value
            logger.info(f"Setting: {key} = {old} -> {value}")
            if key == "stop_loss_pct":
                self._stop_loss_pct = value
            return True
        return False
