"""
Strategy Manager — центральный менеджер всех арбитражных стратегий.
Запускает, останавливает и мониторит все 5 стратегий одновременно.

Режимы работы:
- monitoring_only=True  → только сканирование, уведомления НЕ отправляются
- dry_run_mode=True     → симуляция сделок, уведомления об исполненных сделках
- Реальный режим        → реальные сделки, уведомления об исполненных сделках
"""
import asyncio
import time
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.exchanges import OKXRestClient, HTXRestClient
from arbitrage.core.notifications import NotificationManager
from arbitrage.strategies.base import (
    BaseOpportunity, StrategyType,
    SpotArbitrageOpportunity, FuturesArbitrageOpportunity,
    FundingArbitrageOpportunity, BasisArbitrageOpportunity,
    TriangularArbitrageOpportunity,
)
from arbitrage.strategies.spot_arb import SpotArbitrageMonitor
from arbitrage.strategies.futures_arb import FuturesArbitrageMonitor
from arbitrage.strategies.funding_arb import FundingArbitrageMonitor
from arbitrage.strategies.basis_arb import BasisArbitrageMonitor
from arbitrage.strategies.triangular_arb import TriangularArbitrageMonitor
from arbitrage.strategies.trade_executor import TradeExecutor, TradeRecord

logger = get_arbitrage_logger("strategy_manager")

# Интервалы опроса (в секундах)
FUTURES_UPDATE_INTERVAL = 2
SPOT_UPDATE_INTERVAL = 3
FUNDING_UPDATE_INTERVAL = 60    # Funding меняется редко
BASIS_UPDATE_INTERVAL = 5
TRIANGULAR_UPDATE_INTERVAL = 3

# Максимальное время жизни открытой сделки (сек) — защита от «вечных» позиций
MAX_TRADE_DURATION = 3600  # 1 час


class StrategyManager:
    """
    Управляет всеми арбитражными стратегиями.
    Каждая стратегия работает независимо в отдельном asyncio-таске.

    Поведение:
    - Находит арбитражную возможность
    - Если она держится >= min_opportunity_lifetime и нет открытой сделки → открывает сделку
    - Мониторит условия выхода
    - Закрывает сделку → отправляет уведомление с P&L
    """

    def __init__(
        self,
        config: ArbitrageConfig,
        okx_client: OKXRestClient,
        htx_client: HTXRestClient,
        notification_manager: NotificationManager,
        enabled_strategies: Optional[Set[StrategyType]] = None,
    ):
        self.config = config
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.notifications = notification_manager
        self.is_running = False

        # Если не указаны явно — включены все стратегии
        self.enabled_strategies: Set[StrategyType] = enabled_strategies or set(StrategyType)

        # Создаём мониторы стратегий
        self.spot_monitor = SpotArbitrageMonitor(
            okx_client, htx_client,
            min_profit_pct=getattr(config, 'min_spot_profit', config.min_spread),
        )
        self.futures_monitor = FuturesArbitrageMonitor(
            okx_client, htx_client,
            min_spread_pct=config.min_spread,
        )
        self.funding_monitor = FundingArbitrageMonitor(
            okx_client, htx_client,
            min_net_funding_pct=getattr(config, 'min_funding_diff', 0.02),
        )
        self.basis_monitor = BasisArbitrageMonitor(
            okx_client, htx_client,
            min_basis_pct=getattr(config, 'min_basis', 0.15),
        )
        self.triangular_monitor = TriangularArbitrageMonitor(
            okx_client, htx_client,
            min_net_profit_pct=getattr(config, 'min_triangular_profit', 0.05),
        )

        # Исполнитель сделок
        self.executor = TradeExecutor(config, okx_client, htx_client)

        # Отслеживание возможностей {(strategy, symbol): (opportunity, first_seen_time)}
        self._pending: Dict[tuple, Tuple[BaseOpportunity, float]] = {}

        # Открытые сделки {(strategy, symbol): TradeRecord}
        self._open_trades: Dict[tuple, TradeRecord] = {}

        # Задачи
        self._tasks: List[asyncio.Task] = []

        # Статистика
        self.stats: Dict[str, Dict[str, int]] = {
            s.value: {"opened": 0, "closed": 0, "profitable": 0}
            for s in StrategyType
        }

    async def initialize(self) -> None:
        """Инициализация всех стратегий"""
        logger.info("Initializing strategy manager...")

        init_tasks = []
        if StrategyType.SPOT_ARB in self.enabled_strategies:
            init_tasks.append(self.spot_monitor.initialize())
        if StrategyType.FUTURES_ARB in self.enabled_strategies:
            init_tasks.append(self.futures_monitor.initialize())
        if StrategyType.FUNDING_ARB in self.enabled_strategies:
            init_tasks.append(self.funding_monitor.update_funding_rates())
        if StrategyType.BASIS_ARB in self.enabled_strategies:
            init_tasks.append(self.basis_monitor.initialize())
        if StrategyType.TRIANGULAR in self.enabled_strategies:
            init_tasks.append(self.triangular_monitor.initialize())

        results = await asyncio.gather(*init_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Strategy init error: {r}")

        logger.info(
            f"Strategy manager initialized. "
            f"Enabled: {[s.value for s in self.enabled_strategies]}"
        )

    def enable_strategy(self, strategy: StrategyType) -> None:
        self.enabled_strategies.add(strategy)
        logger.info(f"Strategy {strategy.value} enabled")

    def disable_strategy(self, strategy: StrategyType) -> None:
        self.enabled_strategies.discard(strategy)
        logger.info(f"Strategy {strategy.value} disabled")

    async def start(self) -> None:
        """Запустить все включённые стратегии"""
        self.is_running = True
        logger.info("Starting strategy manager")

        if StrategyType.SPOT_ARB in self.enabled_strategies:
            self._tasks.append(asyncio.create_task(self._run_spot_arb()))

        if StrategyType.FUTURES_ARB in self.enabled_strategies:
            self._tasks.append(asyncio.create_task(self._run_futures_arb()))

        if StrategyType.FUNDING_ARB in self.enabled_strategies:
            self._tasks.append(asyncio.create_task(self._run_funding_arb()))

        if StrategyType.BASIS_ARB in self.enabled_strategies:
            self._tasks.append(asyncio.create_task(self._run_basis_arb()))

        if StrategyType.TRIANGULAR in self.enabled_strategies:
            self._tasks.append(asyncio.create_task(self._run_triangular_arb()))

        # Ждём завершения всех задач
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        """Остановить все стратегии"""
        self.is_running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()
        logger.info("Strategy manager stopped")

    # ─── Loops ──────────────────────────────────────────────────────────────

    async def _run_spot_arb(self) -> None:
        logger.info("Spot arbitrage loop started")
        while self.is_running:
            try:
                await self.spot_monitor.update_prices()
                opps = self.spot_monitor.calculate_opportunities()
                await self._process_opportunities(opps, StrategyType.SPOT_ARB)
                await self._check_spot_exits()
                await asyncio.sleep(SPOT_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Spot arb loop error: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _run_futures_arb(self) -> None:
        logger.info("Futures arbitrage loop started")
        while self.is_running:
            try:
                await self.futures_monitor.update_prices()
                opps = self.futures_monitor.calculate_opportunities()
                await self._process_opportunities(opps, StrategyType.FUTURES_ARB)
                await self._check_futures_exits()
                await asyncio.sleep(FUTURES_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Futures arb loop error: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _run_funding_arb(self) -> None:
        logger.info("Funding arbitrage loop started")
        while self.is_running:
            try:
                await self.funding_monitor.update_funding_rates()
                opps = self.funding_monitor.calculate_opportunities()
                await self._process_opportunities(opps, StrategyType.FUNDING_ARB)
                await self._check_funding_exits()
                await asyncio.sleep(FUNDING_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Funding arb loop error: {e}", exc_info=True)
                await asyncio.sleep(30)

    async def _run_basis_arb(self) -> None:
        logger.info("Basis arbitrage loop started")
        while self.is_running:
            try:
                await self.basis_monitor.update_prices()
                opps = self.basis_monitor.calculate_opportunities()
                await self._process_opportunities(opps, StrategyType.BASIS_ARB)
                await self._check_basis_exits()
                await asyncio.sleep(BASIS_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Basis arb loop error: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _run_triangular_arb(self) -> None:
        logger.info("Triangular arbitrage loop started")
        while self.is_running:
            try:
                await self.triangular_monitor.update_prices()
                opps = self.triangular_monitor.calculate_opportunities()
                await self._process_opportunities(opps, StrategyType.TRIANGULAR)
                await self._check_triangular_exits()
                await asyncio.sleep(TRIANGULAR_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Triangular arb loop error: {e}", exc_info=True)
                await asyncio.sleep(10)

    # ─── Core Logic ─────────────────────────────────────────────────────────

    async def _process_opportunities(
        self,
        opportunities: List[BaseOpportunity],
        strategy: StrategyType,
    ) -> None:
        """
        Обрабатываем найденные возможности:
        - Новые: начинаем отсчёт времени
        - Держатся >= min_opportunity_lifetime и нет открытой сделки: пробуем открыть сделку
        - Исчезнувшие: удаляем из pending
        """
        current_time = time.time()
        current_keys = {(strategy, opp.symbol) for opp in opportunities}

        # Убираем исчезнувшие возможности из pending (не трогаем открытые сделки)
        to_delete = [
            k for k in list(self._pending)
            if k[0] == strategy and k not in current_keys
        ]
        for k in to_delete:
            del self._pending[k]

        # Обрабатываем текущие возможности
        for opp in opportunities:
            key = (strategy, opp.symbol)

            if key not in self._pending:
                # Новая возможность — начинаем отсчёт
                self._pending[key] = (opp, current_time)
                logger.info(
                    f"New {strategy.display_name} opportunity: "
                    f"{opp.symbol} {opp.profit_pct:.3f}%"
                )
            else:
                # Возможность уже в очереди
                stored_opp, start_time = self._pending[key]
                duration = current_time - start_time
                min_lifetime = self.config.min_opportunity_lifetime

                # Обновляем данные возможности (актуализируем цены)
                self._pending[key] = (opp, start_time)

                # Достаточно долго держится И нет открытой сделки?
                if duration >= min_lifetime and key not in self._open_trades:
                    await self._try_open_trade(key, opp, strategy)

    async def _try_open_trade(
        self,
        key: tuple,
        opp: BaseOpportunity,
        strategy: StrategyType,
    ) -> None:
        """Попытаться открыть сделку по возможности"""
        # Извлекаем параметры в зависимости от типа
        params = self._extract_trade_params(opp)
        if params is None:
            return

        long_exchange, short_exchange, long_price, short_price, spread_pct = params
        size = self.config.position_size

        success, trade, msg = await self.executor.open_trade(
            strategy=strategy.value,
            symbol=opp.symbol,
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            long_price=long_price,
            short_price=short_price,
            size=size,
            spread_pct=spread_pct,
        )

        if success and trade is not None:
            self._open_trades[key] = trade
            self.stats[strategy.value]["opened"] += 1
            logger.info(
                f"[{strategy.value}] Trade opened: {opp.symbol} "
                f"spread={spread_pct:.3f}% dry_run={trade.dry_run}"
            )
        else:
            logger.warning(f"[{strategy.value}] Failed to open trade {opp.symbol}: {msg}")

    def _extract_trade_params(
        self, opp: BaseOpportunity
    ) -> Optional[Tuple[str, str, float, float, float]]:
        """
        Извлечь (long_exchange, short_exchange, long_price, short_price, spread_pct)
        из объекта возможности.
        """
        try:
            if isinstance(opp, SpotArbitrageOpportunity):
                # buy_exchange — там покупаем (LONG), sell_exchange — там продаём (SHORT)
                return (
                    opp.buy_exchange,
                    opp.sell_exchange,
                    opp.buy_price,
                    opp.sell_price,
                    opp.profit_pct,
                )
            elif isinstance(opp, FuturesArbitrageOpportunity):
                return (
                    opp.long_exchange,
                    opp.short_exchange,
                    opp.long_price,
                    opp.short_price,
                    opp.profit_pct,
                )
            elif isinstance(opp, FundingArbitrageOpportunity):
                # Для funding arb: LONG на той бирже, где платим меньше funding
                return (
                    opp.long_exchange,
                    opp.short_exchange,
                    0.0,  # цены не критичны для funding — открываем по рынку
                    0.0,
                    opp.net_funding_8h,
                )
            elif isinstance(opp, BasisArbitrageOpportunity):
                if opp.direction == "cash_and_carry":
                    # Покупаем спот (LONG spot_exchange), шортим фьючерс (SHORT futures_exchange)
                    return (
                        opp.spot_exchange,
                        opp.futures_exchange,
                        opp.spot_price,
                        opp.futures_price,
                        abs(opp.basis_pct),
                    )
                else:
                    # Reverse: продаём спот (SHORT spot_exchange), лонгим фьючерс (LONG futures_exchange)
                    return (
                        opp.futures_exchange,
                        opp.spot_exchange,
                        opp.futures_price,
                        opp.spot_price,
                        abs(opp.basis_pct),
                    )
            elif isinstance(opp, TriangularArbitrageOpportunity):
                # Треугольный арбитраж — внутри одной биржи, не требует двух ног
                # Исполнение специфично, пропускаем реальное открытие
                return None
            else:
                return None
        except AttributeError as e:
            logger.error(f"Failed to extract trade params: {e}")
            return None

    # ─── Exit Condition Checks ──────────────────────────────────────────────

    async def _check_futures_exits(self) -> None:
        """
        Выход из фьючерсных позиций:
        спред закрылся <= exit_threshold ИЛИ превышено MAX_TRADE_DURATION
        """
        exit_threshold = self.config.exit_threshold
        keys = [k for k in self._open_trades if k[0] == StrategyType.FUTURES_ARB]

        if not keys:
            return

        # Актуальные спреды
        all_spreads = self.futures_monitor.get_all_spreads()
        spread_map = {s["symbol"]: s.get("best_spread", 999.0) for s in all_spreads}

        for key in keys:
            trade = self._open_trades[key]
            symbol = key[1]
            current_spread = spread_map.get(symbol, 0.0)
            duration = trade.duration_seconds()

            should_exit = (
                current_spread <= exit_threshold
                or duration >= MAX_TRADE_DURATION
            )

            if should_exit:
                reason = "spread_closed" if current_spread <= exit_threshold else "timeout"
                # Получаем текущие цены для выхода
                exit_prices = self._get_current_prices_from_spreads(all_spreads, symbol)
                await self._do_close_trade(
                    key, trade, exit_prices[0], exit_prices[1], current_spread, reason
                )

    async def _check_spot_exits(self) -> None:
        """Выход из спот-позиций"""
        exit_threshold = self.config.exit_threshold
        keys = [k for k in self._open_trades if k[0] == StrategyType.SPOT_ARB]

        if not keys:
            return

        all_spreads = self.spot_monitor.get_all_spreads()
        spread_map = {s["symbol"]: s.get("net_profit", 999.0) for s in all_spreads}

        for key in keys:
            trade = self._open_trades[key]
            symbol = key[1]
            current_spread = spread_map.get(symbol, 0.0)
            duration = trade.duration_seconds()

            if current_spread <= exit_threshold or duration >= MAX_TRADE_DURATION:
                reason = "spread_closed" if current_spread <= exit_threshold else "timeout"
                exit_prices = self._get_current_prices_from_spreads(all_spreads, symbol)
                await self._do_close_trade(
                    key, trade, exit_prices[0], exit_prices[1], current_spread, reason
                )

    async def _check_funding_exits(self) -> None:
        """
        Выход из funding arb:
        Если разница funding < min_funding_diff/2 ИЛИ превышено MAX_TRADE_DURATION
        """
        min_diff = getattr(self.config, 'min_funding_diff', 0.02)
        keys = [k for k in self._open_trades if k[0] == StrategyType.FUNDING_ARB]

        if not keys:
            return

        all_spreads = self.funding_monitor.get_all_spreads()
        spread_map = {s["symbol"]: s.get("net_8h", 999.0) for s in all_spreads}

        for key in keys:
            trade = self._open_trades[key]
            symbol = key[1]
            current_diff = spread_map.get(symbol, 0.0)
            duration = trade.duration_seconds()

            # Выходим если разница funding схлопнулась или прошёл час
            if current_diff < min_diff / 2 or duration >= MAX_TRADE_DURATION:
                reason = "funding_converged" if current_diff < min_diff / 2 else "timeout"
                await self._do_close_trade(
                    key, trade, 0.0, 0.0, current_diff, reason
                )

    async def _check_basis_exits(self) -> None:
        """
        Выход из basis arb:
        Когда базис сошёлся (basis ≈ 0) ИЛИ превышено MAX_TRADE_DURATION
        """
        keys = [k for k in self._open_trades if k[0] == StrategyType.BASIS_ARB]

        if not keys:
            return

        all_spreads = self.basis_monitor.get_all_spreads()
        spread_map = {s["symbol"]: abs(s.get("basis_pct", 999.0)) for s in all_spreads}

        for key in keys:
            trade = self._open_trades[key]
            symbol = key[1]
            current_basis = spread_map.get(symbol, 0.0)
            duration = trade.duration_seconds()
            entry_basis = trade.entry_spread_pct

            # Выходим если базис закрылся до exit_threshold от начального
            converged = current_basis <= self.config.exit_threshold
            timed_out = duration >= MAX_TRADE_DURATION

            if converged or timed_out:
                reason = "basis_converged" if converged else "timeout"
                await self._do_close_trade(
                    key, trade, 0.0, 0.0, current_basis, reason
                )

    async def _check_triangular_exits(self) -> None:
        """Треугольный арбитраж — закрываем по timeout"""
        keys = [k for k in self._open_trades if k[0] == StrategyType.TRIANGULAR]
        for key in keys:
            trade = self._open_trades[key]
            if trade.duration_seconds() >= 60:  # Треугольный — быстрая сделка
                await self._do_close_trade(key, trade, 0.0, 0.0, 0.0, "completed")

    async def _do_close_trade(
        self,
        key: tuple,
        trade: TradeRecord,
        exit_long_price: float,
        exit_short_price: float,
        exit_spread_pct: float,
        reason: str,
    ) -> None:
        """Закрыть сделку и отправить уведомление с P&L"""
        success, msg = await self.executor.close_trade(
            trade=trade,
            exit_long_price=exit_long_price,
            exit_short_price=exit_short_price,
            exit_spread_pct=exit_spread_pct,
            reason=reason,
        )

        if success:
            # Удаляем из открытых
            del self._open_trades[key]
            # Обновляем статистику
            strategy_val = key[0].value
            self.stats[strategy_val]["closed"] += 1
            if trade.net_pnl > 0:
                self.stats[strategy_val]["profitable"] += 1
            # Отправляем уведомление о завершённой сделке
            await self._send_trade_completion_notification(trade)
        else:
            logger.error(f"Failed to close trade {key}: {msg}")

    def _get_current_prices_from_spreads(
        self,
        spreads: list,
        symbol: str
    ) -> Tuple[float, float]:
        """Получить текущие цены для выхода"""
        for s in spreads:
            if s.get("symbol") == symbol:
                return (
                    s.get("long_price", 0.0) or s.get("buy_price", 0.0),
                    s.get("short_price", 0.0) or s.get("sell_price", 0.0),
                )
        return 0.0, 0.0

    # ─── Trade Completion Notification ──────────────────────────────────────

    async def _send_trade_completion_notification(self, trade: TradeRecord) -> None:
        """Отправить уведомление о завершённой сделке с финансовыми результатами"""
        try:
            result_emoji = "✅" if trade.net_pnl >= 0 else "❌"
            mode_str = " [DRY RUN]" if trade.dry_run else ""

            # Строки цен (если 0.0 — не показываем)
            entry_prices_str = ""
            if trade.entry_long_price > 0 and trade.entry_short_price > 0:
                entry_prices_str = (
                    f"📥 Вход:\n"
                    f"   LONG {trade.long_exchange.upper()}: ${trade.entry_long_price:,.4f}\n"
                    f"   SHORT {trade.short_exchange.upper()}: ${trade.entry_short_price:,.4f}\n"
                )

            exit_prices_str = ""
            if trade.exit_long_price > 0 and trade.exit_short_price > 0:
                exit_prices_str = (
                    f"📤 Выход:\n"
                    f"   LONG {trade.long_exchange.upper()}: ${trade.exit_long_price:,.4f}\n"
                    f"   SHORT {trade.short_exchange.upper()}: ${trade.exit_short_price:,.4f}\n"
                )

            spread_str = (
                f"📊 Спред вход: {trade.entry_spread_pct:+.3f}%\n"
                f"📊 Спред выход: {trade.exit_spread_pct:+.3f}%\n"
            )

            pnl_str = (
                f"💵 Валовой P&L: {trade.gross_pnl:+.4f} USDT\n"
                f"💸 Комиссии: -{trade.total_fees:.4f} USDT\n"
                f"{'✅' if trade.net_pnl >= 0 else '❌'} <b>Чистый P&L: {trade.net_pnl:+.4f} USDT</b>\n"
            )

            # Стратегия → имя и эмодзи
            strategy_display = {
                "spot_arb": "🔄 Спот-арбитраж",
                "futures_arb": "📊 Фьючерсный арбитраж",
                "funding_arb": "💸 Funding Rate арбитраж",
                "basis_arb": "⚖️ Basis арбитраж",
                "triangular": "🔺 Треугольный арбитраж",
            }.get(trade.strategy, trade.strategy)

            msg = (
                f"{result_emoji} <b>Сделка завершена{mode_str}</b>\n\n"
                f"🎯 Стратегия: {strategy_display}\n"
                f"💱 Пара: <b>{trade.symbol}</b>\n"
                f"📦 Объём: {trade.size} контрактов\n\n"
                f"{entry_prices_str}"
                f"{exit_prices_str}"
                f"{spread_str}\n"
                f"{pnl_str}\n"
                f"⏱ Длительность: {trade.duration_str()}\n"
                f"🕐 Открыта: {trade.entry_time_str()}\n"
                f"🕑 Закрыта: {trade.exit_time_str()}"
            )
            await self.notifications.send(msg)

        except Exception as e:
            logger.error(f"Trade notification error: {e}", exc_info=True)

    # ─── Scan (без порога, без сделок) ──────────────────────────────────────

    async def scan_all(self) -> Dict[str, list]:
        """
        Однократный скан всех стратегий для кнопки "Сканировать".
        Возвращает топ-результаты по каждой стратегии.
        """
        results = {}

        await asyncio.gather(
            self.spot_monitor.update_prices() if StrategyType.SPOT_ARB in self.enabled_strategies else asyncio.sleep(0),
            self.futures_monitor.update_prices() if StrategyType.FUTURES_ARB in self.enabled_strategies else asyncio.sleep(0),
            self.funding_monitor.update_funding_rates() if StrategyType.FUNDING_ARB in self.enabled_strategies else asyncio.sleep(0),
            self.basis_monitor.update_prices() if StrategyType.BASIS_ARB in self.enabled_strategies else asyncio.sleep(0),
            self.triangular_monitor.update_prices() if StrategyType.TRIANGULAR in self.enabled_strategies else asyncio.sleep(0),
            return_exceptions=True
        )

        if StrategyType.SPOT_ARB in self.enabled_strategies:
            results["spot"] = self.spot_monitor.get_all_spreads()[:5]

        if StrategyType.FUTURES_ARB in self.enabled_strategies:
            results["futures"] = self.futures_monitor.get_all_spreads()[:5]

        if StrategyType.FUNDING_ARB in self.enabled_strategies:
            results["funding"] = self.funding_monitor.get_all_spreads()[:5]

        if StrategyType.BASIS_ARB in self.enabled_strategies:
            results["basis"] = self.basis_monitor.get_all_spreads()[:5]

        if StrategyType.TRIANGULAR in self.enabled_strategies:
            results["triangular"] = self.triangular_monitor.get_all_spreads()[:5]

        return results

    def get_status(self) -> Dict[str, any]:
        """Статус всех стратегий"""
        open_by_strategy: Dict[str, int] = {s.value: 0 for s in StrategyType}
        for (strategy, _) in self._open_trades:
            open_by_strategy[strategy.value] += 1

        return {
            "is_running": self.is_running,
            "enabled": [s.value for s in self.enabled_strategies],
            "open_trades": open_by_strategy,
            "stats": self.stats,
            "mode": (
                "monitoring_only" if self.config.monitoring_only
                else ("dry_run" if self.config.dry_run_mode else "real")
            ),
        }
