"""
Мульти-парный арбитражный движок для мониторинга всех торговых пар
"""
import asyncio
from typing import Dict, List, Optional, Set, Tuple
import time
from datetime import datetime
from dataclasses import dataclass, field

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig, calculate_spread
from arbitrage.core.state import BotState
from arbitrage.core.risk import RiskManager
from arbitrage.core.execution import ExecutionManager
from arbitrage.core.notifications import NotificationManager
from arbitrage.exchanges import OKXRestClient, HTXRestClient

logger = get_arbitrage_logger("multi_pair_arbitrage")


@dataclass
class PairSpread:
    """Информация о спреде для пары"""
    symbol: str
    spread: float
    direction: str  # "okx_long" или "htx_long"
    okx_bid: float
    okx_ask: float
    htx_bid: float
    htx_ask: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def get_long_exchange(self) -> str:
        return "okx" if self.direction == "okx_long" else "htx"

    def get_short_exchange(self) -> str:
        return "htx" if self.direction == "okx_long" else "okx"

    def get_long_price(self) -> float:
        return self.okx_ask if self.direction == "okx_long" else self.htx_ask

    def get_short_price(self) -> float:
        return self.htx_bid if self.direction == "okx_long" else self.okx_bid


class MultiPairArbitrageEngine:
    """
    Движок для мониторинга арбитражных возможностей на всех торговых парах
    """

    def __init__(
        self,
        config: ArbitrageConfig,
        state: BotState,
        risk_manager: RiskManager,
        execution_manager: ExecutionManager,
        okx_client: OKXRestClient,
        htx_client: HTXRestClient,
        notification_manager: Optional[NotificationManager] = None
    ):
        self.config = config
        self.state = state
        self.risk = risk_manager
        self.execution = execution_manager
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.notifications = notification_manager or NotificationManager()

        # Кэш цен для всех пар
        self.okx_prices: Dict[str, Dict[str, float]] = {}  # {symbol: {bid, ask}}
        self.htx_prices: Dict[str, Dict[str, float]] = {}  # {symbol: {bid, ask}}

        # Список отслеживаемых пар
        self.monitored_pairs: Set[str] = set()

        # Лучшие спреды
        self.best_spreads: List[PairSpread] = []

        # Отслеживание активных возможностей {symbol: (PairSpread, start_time)}
        self.active_opportunities: Dict[str, Tuple[PairSpread, float]] = {}

        # Время последнего обновления
        self.last_update_time = 0
        self.update_interval = config.update_interval  # Интервал обновления цен (из конфига)

        # Минимальные требования для фильтрации пар
        self.min_volume_usdt = 1000000  # Минимум $1M объем за 24ч
        self.min_spread = config.min_spread  # Минимальный интересный спред (из конфига)

        # Порог для уведомления об изменении спреда (в процентных пунктах)
        self.spread_change_threshold = config.spread_change_threshold  # Из конфига

    async def initialize(self) -> None:
        """Инициализация - получение списка торговых пар"""
        logger.info("Initializing multi-pair arbitrage engine (OKX <-> HTX)")

        try:
            # Получаем список инструментов с обеих бирж
            okx_instruments = await self._get_okx_instruments()
            htx_instruments = await self._get_htx_instruments()

            # Находим общие пары
            common_pairs = okx_instruments & htx_instruments

            logger.info(f"Found {len(okx_instruments)} OKX instruments")
            logger.info(f"Found {len(htx_instruments)} HTX instruments")
            logger.info(f"Common pairs: {len(common_pairs)}")

            # Фильтруем пары по объему и другим критериям
            self.monitored_pairs = await self._filter_pairs(common_pairs)

            logger.info(f"Monitoring {len(self.monitored_pairs)} pairs: {sorted(self.monitored_pairs)}")

        except Exception as e:
            logger.error(f"Initialization error: {e}", exc_info=True)
            raise

    async def _get_okx_instruments(self) -> Set[str]:
        """Получить список торговых инструментов с OKX"""
        try:
            result = await self.okx_client.get_instruments(inst_type="SWAP")

            if result.get("code") != "0":
                logger.error(f"OKX instruments error: {result}")
                return set()

            instruments = set()
            for inst in result.get("data", []):
                inst_id = inst.get("instId", "")
                # Преобразуем BTC-USDT-SWAP -> BTCUSDT
                if "-USDT-SWAP" in inst_id:
                    symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                    instruments.add(symbol)

            return instruments

        except Exception as e:
            logger.error(f"Error getting OKX instruments: {e}", exc_info=True)
            return set()

    async def _get_htx_instruments(self) -> Set[str]:
        """Получить список торговых инструментов с HTX"""
        try:
            result = await self.htx_client.get_instruments()

            # HTX возвращает {"status": "ok", "data": [...]}
            if result.get("status") != "ok":
                logger.error(f"HTX instruments error: {result}")
                return set()

            instruments = set()
            for inst in result.get("data", []):
                contract_code = inst.get("contract_code", "")
                # Преобразуем BTC-USDT -> BTCUSDT
                if contract_code.endswith("-USDT"):
                    symbol = contract_code.replace("-", "")
                    instruments.add(symbol)

            return instruments

        except Exception as e:
            logger.error(f"Error getting HTX instruments: {e}", exc_info=True)
            return set()

    async def _filter_pairs(self, pairs: Set[str]) -> Set[str]:
        """
        Фильтровать пары по объему и ликвидности

        Args:
            pairs: Множество символов для фильтрации

        Returns:
            Отфильтрованное множество символов
        """
        # В mock режиме возвращаем топ-10 популярных пар
        if self.config.mock_mode:
            popular_pairs = {
                "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                "ADAUSDT", "DOGEUSDT", "MATICUSDT", "DOTUSDT", "AVAXUSDT"
            }
            return popular_pairs & pairs

        # В real режиме - фильтруем по объему
        filtered = set()

        try:
            # Получаем тикеры с обеих бирж
            okx_tickers_result = await self.okx_client.get_tickers(inst_type="SWAP")
            htx_tickers_result = await self.htx_client.get_tickers()

            # Парсим объемы OKX
            okx_volumes = {}
            if okx_tickers_result.get("code") == "0":
                for ticker in okx_tickers_result.get("data", []):
                    inst_id = ticker.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        volume = float(ticker.get("vol24h", 0))
                        okx_volumes[symbol] = volume

            # Парсим объемы HTX
            htx_volumes = {}
            if htx_tickers_result.get("status") == "ok":
                for ticker in htx_tickers_result.get("data", []):
                    contract_code = ticker.get("contract_code", "")
                    if contract_code.endswith("-USDT"):
                        symbol = contract_code.replace("-", "")
                        volume = float(ticker.get("amount", 0))
                        htx_volumes[symbol] = volume

            # Фильтруем по минимальному объему
            for pair in pairs:
                okx_vol = okx_volumes.get(pair, 0)
                htx_vol = htx_volumes.get(pair, 0)
                avg_volume = (okx_vol + htx_vol) / 2

                if avg_volume >= self.min_volume_usdt:
                    filtered.add(pair)

            # Если слишком мало пар, берем топ-20 по объему
            if len(filtered) < 10:
                all_pairs_with_volume = []
                for pair in pairs:
                    okx_vol = okx_volumes.get(pair, 0)
                    htx_vol = htx_volumes.get(pair, 0)
                    avg_volume = (okx_vol + htx_vol) / 2
                    all_pairs_with_volume.append((pair, avg_volume))

                all_pairs_with_volume.sort(key=lambda x: x[1], reverse=True)
                filtered = set([pair for pair, _ in all_pairs_with_volume[:20]])

        except Exception as e:
            logger.error(f"Error filtering pairs: {e}", exc_info=True)
            # Fallback на популярные пары
            popular_pairs = {
                "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                "ADAUSDT", "DOGEUSDT", "MATICUSDT", "DOTUSDT", "AVAXUSDT"
            }
            filtered = popular_pairs & pairs

        return filtered

    async def update_prices(self) -> None:
        """Обновить цены для всех отслеживаемых пар"""
        try:
            # Получаем тикеры с обеих бирж параллельно
            okx_tickers_task = asyncio.create_task(
                self.okx_client.get_tickers(inst_type="SWAP")
            )
            htx_tickers_task = asyncio.create_task(
                self.htx_client.get_tickers()
            )

            okx_result, htx_result = await asyncio.gather(
                okx_tickers_task, htx_tickers_task, return_exceptions=True
            )

            # Обрабатываем OKX тикеры
            okx_updated = 0
            if isinstance(okx_result, Exception):
                logger.error(f"OKX tickers request failed: {okx_result}")
            elif okx_result.get("code") != "0":
                logger.error(f"OKX tickers API error: code={okx_result.get('code')} msg={okx_result.get('msg')}")
            else:
                for ticker in okx_result.get("data", []):
                    inst_id = ticker.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        symbol = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        if symbol in self.monitored_pairs:
                            try:
                                bid = float(ticker.get("bidPx") or 0)
                                ask = float(ticker.get("askPx") or 0)
                                if bid > 0 and ask > 0:
                                    self.okx_prices[symbol] = {"bid": bid, "ask": ask}
                                    okx_updated += 1
                            except (ValueError, TypeError):
                                pass

            # Обрабатываем HTX тикеры
            # HTX batch_merged format: {"status": "ok", "data": [{"contract_code": "BTC-USDT", "bid": [price, qty], "ask": [...]}]}
            htx_updated = 0
            if isinstance(htx_result, Exception):
                logger.error(f"HTX tickers request failed: {htx_result}")
            elif htx_result.get("status") != "ok":
                logger.error(f"HTX tickers API error: status={htx_result.get('status')}")
            else:
                for ticker in htx_result.get("data", []):
                    contract_code = ticker.get("contract_code", "")
                    if contract_code.endswith("-USDT"):
                        symbol = contract_code.replace("-", "")
                        if symbol in self.monitored_pairs:
                            try:
                                bid_data = ticker.get("bid", [0, 0])
                                ask_data = ticker.get("ask", [0, 0])
                                bid = float(bid_data[0]) if bid_data else 0
                                ask = float(ask_data[0]) if ask_data else 0
                                if bid > 0 and ask > 0:
                                    self.htx_prices[symbol] = {"bid": bid, "ask": ask}
                                    htx_updated += 1
                            except (ValueError, TypeError, IndexError):
                                pass

            logger.info(
                f"Prices updated: OKX={okx_updated}/{len(self.monitored_pairs)} pairs, "
                f"HTX={htx_updated}/{len(self.monitored_pairs)} pairs"
            )

        except Exception as e:
            logger.error(f"Error updating prices: {e}", exc_info=True)

    async def calculate_spreads(self) -> List[PairSpread]:
        """
        Рассчитать спреды для всех пар

        Returns:
            Список PairSpread, отсортированный по убыванию спреда
        """
        spreads = []

        for symbol in self.monitored_pairs:
            okx_price = self.okx_prices.get(symbol)
            htx_price = self.htx_prices.get(symbol)

            if not okx_price or not htx_price:
                continue

            okx_bid = okx_price.get("bid", 0)
            okx_ask = okx_price.get("ask", 0)
            htx_bid = htx_price.get("bid", 0)
            htx_ask = htx_price.get("ask", 0)

            if okx_bid == 0 or okx_ask == 0 or htx_bid == 0 or htx_ask == 0:
                continue

            # Spread 1: LONG OKX, SHORT HTX
            spread1 = calculate_spread(htx_bid, okx_ask)

            # Spread 2: LONG HTX, SHORT OKX
            spread2 = calculate_spread(okx_bid, htx_ask)

            # Выбираем лучший спред
            if spread1 > spread2 and spread1 > self.min_spread:
                spreads.append(PairSpread(
                    symbol=symbol,
                    spread=spread1,
                    direction="okx_long",
                    okx_bid=okx_bid,
                    okx_ask=okx_ask,
                    htx_bid=htx_bid,
                    htx_ask=htx_ask
                ))
            elif spread2 > self.min_spread:
                spreads.append(PairSpread(
                    symbol=symbol,
                    spread=spread2,
                    direction="htx_long",
                    okx_bid=okx_bid,
                    okx_ask=okx_ask,
                    htx_bid=htx_bid,
                    htx_ask=htx_ask
                ))

        # Сортируем по убыванию спреда
        spreads.sort(key=lambda x: x.spread, reverse=True)

        return spreads

    async def track_opportunities(self, current_spreads: List[PairSpread]) -> None:
        """
        Отслеживать изменения арбитражных возможностей и отправлять уведомления

        Args:
            current_spreads: Текущий список спредов
        """
        current_time = time.time()

        # Создаем словарь текущих возможностей для быстрого доступа
        current_opps = {spread.symbol: spread for spread in current_spreads}

        # Проверяем изменения в активных возможностях
        for symbol in list(self.active_opportunities.keys()):
            old_spread, start_time = self.active_opportunities[symbol]

            if symbol not in current_opps:
                logger.info(f"Opportunity disappeared: {symbol}")
                del self.active_opportunities[symbol]
            elif current_opps[symbol].spread < self.min_spread:
                logger.info(f"Opportunity weakened: {symbol} ({current_opps[symbol].spread:.3f}%)")
                del self.active_opportunities[symbol]

        # Проверяем новые возможности
        for symbol, spread in current_opps.items():
            if symbol not in self.active_opportunities:
                logger.info(f"New opportunity: {symbol} - {spread.spread:.3f}%")
                self.active_opportunities[symbol] = (spread, current_time)
            else:
                old_spread, start_time = self.active_opportunities[symbol]
                duration = current_time - start_time

                last_notified = getattr(old_spread, '_last_notified', None)

                first_notify = (
                    duration >= self.config.min_opportunity_lifetime
                    and last_notified is None
                )

                renotify_interval = getattr(self.config, 'renotify_interval', 60)
                repeat_notify = (
                    last_notified is not None
                    and (current_time - last_notified) >= renotify_interval
                )

                if first_notify or repeat_notify:
                    logger.info(
                        f"{'Stable' if first_notify else 'Re-notify'} opportunity: "
                        f"{symbol} - {spread.spread:.3f}% (held {duration:.1f}s)"
                    )

                    spread._last_notified = current_time
                    self.active_opportunities[symbol] = (spread, start_time)

                    await self.notifications.notify_opportunity_found(
                        symbol=symbol,
                        spread=spread.spread,
                        long_exchange=spread.get_long_exchange(),
                        short_exchange=spread.get_short_exchange(),
                        long_price=spread.get_long_price(),
                        short_price=spread.get_short_price()
                    )

    async def find_best_opportunities(self, top_n: int = 10) -> List[PairSpread]:
        """
        Найти лучшие арбитражные возможности

        Args:
            top_n: Количество лучших возможностей для возврата

        Returns:
            Список лучших PairSpread
        """
        await self.update_prices()
        all_spreads = await self.calculate_spreads()
        self.best_spreads = all_spreads[:top_n]
        await self.track_opportunities(all_spreads)

        if self.best_spreads:
            logger.info("=" * 60)
            logger.info("TOP ARBITRAGE OPPORTUNITIES (OKX <-> HTX):")
            for i, spread in enumerate(self.best_spreads[:5], 1):
                logger.info(
                    f"{i}. {spread.symbol}: {spread.spread:.3f}% "
                    f"(LONG {spread.get_long_exchange()}, SHORT {spread.get_short_exchange()})"
                )
            logger.info("=" * 60)

        return self.best_spreads

    async def start_monitoring(self) -> None:
        """Запустить постоянный мониторинг всех пар"""
        logger.info("Starting multi-pair monitoring (OKX <-> HTX)")

        while self.state.is_running:
            try:
                opportunities = await self.find_best_opportunities(top_n=10)

                if opportunities:
                    best = opportunities[0]
                    logger.info(
                        f"Best opportunity: {best.symbol} - {best.spread:.3f}% "
                        f"(LONG {best.get_long_exchange()} @ {best.get_long_price()}, "
                        f"SHORT {best.get_short_exchange()} @ {best.get_short_price()})"
                    )

                await asyncio.sleep(self.update_interval)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(5)
