"""
Стратегия 5: Треугольный арбитраж внутри одной биржи

Суть: Находим цикл обмена A → B → C → A, где итоговая сумма > начальной.
Пример: USDT → BTC → ETH → USDT
  1. Покупаем BTC за USDT (BTC-USDT ask)
  2. Продаём BTC за ETH (ETH-BTC bid — т.е. покупаем ETH за BTC)
  3. Продаём ETH за USDT (ETH-USDT bid)
  profit = result - 1.0 - fees (если > 0 → есть арбитраж)

Особенности:
- Очень высокая скорость устранения неэффективностей
- Работает на СПОТОВОМ рынке
- Комиссии: 3 трейда × ~0.1% = ~0.3% total
- Минимальный порог: profit > 0.05% после комиссий
"""
import asyncio
from typing import Dict, List, Optional, Set, Tuple

from arbitrage.utils import get_arbitrage_logger
from arbitrage.strategies.base import TriangularArbitrageOpportunity, StrategyType

logger = get_arbitrage_logger("triangular_arb")

# Комиссии: 3 ноги × 0.1% taker = 0.3%
TAKER_FEE_PER_TRADE = 0.001
NUM_TRADES = 3
TOTAL_FEE = TAKER_FEE_PER_TRADE * NUM_TRADES  # 0.3%
MIN_GROSS_PROFIT = TOTAL_FEE + 0.0005  # Минимум 0.35% gross для прибыли

# Предопределённые треугольные пути для мониторинга
# Формат: (base_quote1, base_quote2, cross_pair)
TRIANGLE_PATHS = [
    # Через BTC
    ("BTCUSDT", "ETHBTC", "ETHUSDT"),    # USDT→BTC→ETH→USDT
    ("BTCUSDT", "BNBBTC", "BNBUSDT"),    # USDT→BTC→BNB→USDT
    ("BTCUSDT", "SOLUSDT", None),         # placeholder — нет прямой кросс-пары SOLBTC на некоторых биржах
    # Через ETH
    ("ETHUSDT", "BNBETH", "BNBUSDT"),    # USDT→ETH→BNB→USDT
    # Через BNB (доступность зависит от биржи)
    ("BNBUSDT", "SOLBNB", "SOLUSDT"),    # USDT→BNB→SOL→USDT
]

# Фиксированные пути с явным указанием операций
# path: список (pair, side) где side = 'buy' или 'sell'
FIXED_PATHS = [
    {
        "name": "USDT→BTC→ETH→USDT",
        "steps": [
            ("BTCUSDT", "buy"),   # Покупаем BTC за USDT
            ("ETHBTC", "buy"),    # Покупаем ETH за BTC
            ("ETHUSDT", "sell"),  # Продаём ETH за USDT
        ]
    },
    {
        "name": "USDT→ETH→BTC→USDT",
        "steps": [
            ("ETHUSDT", "buy"),   # Покупаем ETH за USDT
            ("ETHBTC", "sell"),   # Продаём ETH за BTC
            ("BTCUSDT", "sell"),  # Продаём BTC за USDT
        ]
    },
    {
        "name": "USDT→BTC→BNB→USDT",
        "steps": [
            ("BTCUSDT", "buy"),
            ("BNBBTC", "buy"),
            ("BNBUSDT", "sell"),
        ]
    },
    {
        "name": "USDT→BNB→BTC→USDT",
        "steps": [
            ("BNBUSDT", "buy"),
            ("BNBBTC", "sell"),
            ("BTCUSDT", "sell"),
        ]
    },
    {
        "name": "USDT→BTC→SOL→USDT",
        "steps": [
            ("BTCUSDT", "buy"),
            ("SOLBTC", "buy"),
            ("SOLUSDT", "sell"),
        ]
    },
    {
        "name": "USDT→ETH→BNB→USDT",
        "steps": [
            ("ETHUSDT", "buy"),
            ("BNBETH", "buy"),
            ("BNBUSDT", "sell"),
        ]
    },
    {
        "name": "USDT→USDC→BTC→USDT",
        "steps": [
            ("USDCUSDT", "buy"),  # купить USDC за USDT
            ("BTCUSDC", "buy"),   # купить BTC за USDC
            ("BTCUSDT", "sell"),  # продать BTC за USDT
        ]
    },
]


class TriangularArbitrageMonitor:
    """
    Мониторинг треугольного арбитража на OKX и HTX (спотовый рынок).
    Работает с фиксированными треугольными путями и вычисляет прибыль.
    """

    def __init__(self, okx_client, htx_client, min_net_profit_pct: float = 0.05):
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.min_net_profit_pct = min_net_profit_pct / 100  # переводим в доли

        # Все нужные спотовые пары
        self.required_pairs: Set[str] = set()
        for path in FIXED_PATHS:
            for pair, _ in path["steps"]:
                self.required_pairs.add(pair)

        # Кэш bid/ask {symbol: {bid, ask}}
        self.okx_orderbook: Dict[str, Dict[str, float]] = {}
        self.htx_orderbook: Dict[str, Dict[str, float]] = {}

        # Доступные пары на биржах
        self.okx_available: Set[str] = set()
        self.htx_available: Set[str] = set()

    async def initialize(self) -> None:
        """Определить доступные пары на биржах"""
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_spot_tickers(),
                self.htx_client.get_spot_tickers(),
                return_exceptions=True
            )

            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for t in okx_result.get("data", []):
                    sym = t.get("instId", "").replace("-", "")
                    self.okx_available.add(sym)

            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for t in htx_result.get("data", []):
                    symbol = t.get("symbol", "")
                    self.htx_available.add(symbol.upper())

            # Фильтруем доступные пути
            okx_paths = [p for p in FIXED_PATHS if all(
                pair in self.okx_available for pair, _ in p["steps"]
            )]
            htx_paths = [p for p in FIXED_PATHS if all(
                pair in self.htx_available for pair, _ in p["steps"]
            )]
            logger.info(
                f"Triangular arb: OKX={len(okx_paths)} paths, "
                f"HTX={len(htx_paths)} paths available"
            )
        except Exception as e:
            logger.error(f"TriangularArbitrage init error: {e}", exc_info=True)

    async def update_prices(self) -> None:
        """Обновить спотовые цены для нужных пар"""
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_spot_tickers(),
                self.htx_client.get_spot_tickers(),
                return_exceptions=True
            )

            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for t in okx_result.get("data", []):
                    sym = t.get("instId", "").replace("-", "")
                    if sym not in self.required_pairs:
                        continue
                    try:
                        bid = float(t.get("bidPx") or 0)
                        ask = float(t.get("askPx") or 0)
                        if bid > 0 and ask > 0:
                            self.okx_orderbook[sym] = {"bid": bid, "ask": ask}
                    except (ValueError, TypeError):
                        pass

            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for t in htx_result.get("data", []):
                    sym = t.get("symbol", "").upper().replace("-", "")
                    if sym not in self.required_pairs:
                        continue
                    try:
                        bid = float(t.get("bid", 0))
                        ask = float(t.get("ask", 0))
                        if bid > 0 and ask > 0:
                            self.htx_orderbook[sym] = {"bid": bid, "ask": ask}
                    except (ValueError, TypeError):
                        pass

        except Exception as e:
            logger.error(f"TriangularArbitrage update_prices error: {e}", exc_info=True)

    def _calc_path_profit(
        self,
        path: dict,
        orderbook: Dict[str, Dict[str, float]]
    ) -> Optional[Tuple[float, List[float]]]:
        """
        Рассчитать прибыль по треугольному пути.
        Начинаем с 1.0 USDT и применяем каждый шаг.

        Для BUY пары X/Y: тратим Y, получаем X → price = ask (платим больше)
          amount_x = amount_y / ask
        Для SELL пары X/Y: тратим X, получаем Y → price = bid (получаем меньше)
          amount_y = amount_x * bid
        """
        amount = 1.0
        rates = []

        for pair, side in path["steps"]:
            prices = orderbook.get(pair)
            if not prices:
                return None

            bid = prices["bid"]
            ask = prices["ask"]

            if side == "buy":
                # Платим quote-валюту, получаем base-валюту по ask
                # amount_base = amount_quote / ask
                if ask <= 0:
                    return None
                amount = amount / ask
                rates.append(ask)
            else:
                # Продаём base-валюту, получаем quote-валюту по bid
                # amount_quote = amount_base * bid
                if bid <= 0:
                    return None
                amount = amount * bid
                rates.append(bid)

        # amount теперь итоговое количество USDT
        # Применяем комиссии
        fee_multiplier = (1 - TAKER_FEE_PER_TRADE) ** NUM_TRADES
        final_amount = amount * fee_multiplier
        profit = final_amount - 1.0  # Прибыль в долях
        return profit, rates

    def calculate_opportunities(self) -> List[TriangularArbitrageOpportunity]:
        """Найти треугольные арбитражные возможности"""
        results: List[TriangularArbitrageOpportunity] = []

        for exchange, orderbook in [("okx", self.okx_orderbook), ("htx", self.htx_orderbook)]:
            for path in FIXED_PATHS:
                # Проверяем что все пары есть в стакане
                if not all(pair in orderbook for pair, _ in path["steps"]):
                    continue

                calc = self._calc_path_profit(path, orderbook)
                if calc is None:
                    continue

                profit, rates = calc
                profit_pct = profit * 100

                if profit_pct < self.min_net_profit_pct * 100:
                    continue

                path_names = path["name"].split("→")
                results.append(TriangularArbitrageOpportunity(
                    strategy=StrategyType.TRIANGULAR,
                    symbol=path["name"].replace("USDT→", "").replace("→USDT", ""),
                    profit_pct=profit_pct,
                    exchange=exchange,
                    path=path_names,
                    rates=rates,
                    gross_profit_pct=profit_pct + TOTAL_FEE * 100,
                ))

        results.sort(key=lambda x: x.profit_pct, reverse=True)
        return results

    def get_all_spreads(self) -> List[dict]:
        """Все треугольные пути с расчётом прибыли для Scan."""
        items = []
        for exchange, orderbook in [("okx", self.okx_orderbook), ("htx", self.htx_orderbook)]:
            for path in FIXED_PATHS:
                if not all(pair in orderbook for pair, _ in path["steps"]):
                    continue
                calc = self._calc_path_profit(path, orderbook)
                if calc is None:
                    continue
                profit, _ = calc
                items.append({
                    "name": path["name"],
                    "exchange": exchange,
                    "profit_pct": profit * 100,
                })
        items.sort(key=lambda x: x["profit_pct"], reverse=True)
        return items
