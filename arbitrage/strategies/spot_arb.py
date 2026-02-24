"""
Стратегия 1: Классический спот-арбитраж (Cross-Exchange Spot Arbitrage)

Суть: Покупаем монету дешевле на одной бирже, продаём дороже на другой.
Формула: Spread = (Price_sell - Price_buy) / Price_buy * 100 - fees
"""
import asyncio
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from arbitrage.utils import get_arbitrage_logger
from arbitrage.strategies.base import SpotArbitrageOpportunity, StrategyType

logger = get_arbitrage_logger("spot_arb")

# Комиссии (taker): OKX 0.1%, HTX 0.2% → суммарно ~0.3% на сделку
TAKER_FEE_OKX = 0.001
TAKER_FEE_HTX = 0.002
TOTAL_FEE_RATE = TAKER_FEE_OKX + TAKER_FEE_HTX  # 0.3%


def symbol_to_okx_spot(symbol: str) -> str:
    """BTCUSDT → BTC-USDT"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT"
    return symbol


def okx_spot_to_symbol(inst_id: str) -> str:
    """BTC-USDT → BTCUSDT"""
    return inst_id.replace("-", "")


class SpotArbitrageMonitor:
    """
    Мониторинг классического спот-арбитража между OKX и HTX.
    Сравниваем спотовые цены (bid/ask) и находим межбиржевые расхождения.
    """

    def __init__(self, okx_client, htx_client, min_profit_pct: float = 0.1):
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.min_profit_pct = min_profit_pct  # Минимальная чистая прибыль после комиссий

        # Кэш цен {symbol: {bid, ask}}
        self.okx_prices: Dict[str, Dict[str, float]] = {}
        self.htx_prices: Dict[str, Dict[str, float]] = {}

        # Общие пары
        self.common_pairs: Set[str] = set()

    async def initialize(self) -> None:
        """Инициализация: получение общих спотовых пар"""
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_spot_tickers(),
                self.htx_client.get_spot_tickers(),
                return_exceptions=True
            )

            okx_symbols: Set[str] = set()
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for t in okx_result.get("data", []):
                    inst_id = t.get("instId", "")
                    if inst_id.endswith("-USDT"):
                        sym = okx_spot_to_symbol(inst_id)
                        okx_symbols.add(sym)

            # HTX spot: data[] with symbol (lowercase) like "btcusdt"
            htx_symbols: Set[str] = set()
            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for t in htx_result.get("data", []):
                    sym_raw = t.get("symbol", "")
                    if sym_raw.endswith("usdt"):
                        sym = sym_raw.upper()
                        htx_symbols.add(sym)

            self.common_pairs = okx_symbols & htx_symbols
            logger.info(
                f"Spot arb: OKX={len(okx_symbols)}, HTX={len(htx_symbols)}, "
                f"common={len(self.common_pairs)}"
            )

        except Exception as e:
            logger.error(f"SpotArbitrage init error: {e}", exc_info=True)
            # Fallback на популярные пары
            self.common_pairs = {
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"
            }

    async def update_prices(self) -> Tuple[int, int]:
        """Обновить спотовые цены. Возвращает (okx_count, htx_count)."""
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_spot_tickers(),
                self.htx_client.get_spot_tickers(),
                return_exceptions=True
            )

            okx_count = 0
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for t in okx_result.get("data", []):
                    inst_id = t.get("instId", "")
                    if not inst_id.endswith("-USDT"):
                        continue
                    sym = okx_spot_to_symbol(inst_id)
                    if sym not in self.common_pairs:
                        continue
                    try:
                        bid = float(t.get("bidPx") or 0)
                        ask = float(t.get("askPx") or 0)
                        if bid > 0 and ask > 0:
                            self.okx_prices[sym] = {"bid": bid, "ask": ask}
                            okx_count += 1
                    except (ValueError, TypeError):
                        pass

            # HTX spot: {"symbol": "btcusdt", "bid": price, "ask": price}
            htx_count = 0
            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for t in htx_result.get("data", []):
                    sym = t.get("symbol", "").upper()
                    if sym not in self.common_pairs:
                        continue
                    try:
                        bid = float(t.get("bid") or 0)
                        ask = float(t.get("ask") or 0)
                        if bid > 0 and ask > 0:
                            self.htx_prices[sym] = {"bid": bid, "ask": ask}
                            htx_count += 1
                    except (ValueError, TypeError):
                        pass

            return okx_count, htx_count

        except Exception as e:
            logger.error(f"SpotArbitrage update_prices error: {e}", exc_info=True)
            return 0, 0

    def calculate_opportunities(self) -> List[SpotArbitrageOpportunity]:
        """
        Рассчитать спот-арбитражные возможности.

        Для каждой пары проверяем два направления:
          Dir1: Покупаем на OKX (okx_ask), продаём на HTX (htx_bid)
                profit = (htx_bid - okx_ask) / okx_ask * 100 - total_fees
          Dir2: Покупаем на HTX (htx_ask), продаём на OKX (okx_bid)
                profit = (okx_bid - htx_ask) / htx_ask * 100 - total_fees
        """
        results: List[SpotArbitrageOpportunity] = []
        fee_pct = TOTAL_FEE_RATE * 100  # 0.3%

        for symbol in self.common_pairs:
            okx = self.okx_prices.get(symbol)
            htx = self.htx_prices.get(symbol)
            if not okx or not htx:
                continue

            okx_bid = okx["bid"]
            okx_ask = okx["ask"]
            htx_bid = htx["bid"]
            htx_ask = htx["ask"]

            # Direction 1: BUY on OKX, SELL on HTX
            if okx_ask > 0:
                spread1_raw = (htx_bid - okx_ask) / okx_ask * 100
                net1 = spread1_raw - fee_pct
                if net1 >= self.min_profit_pct:
                    results.append(SpotArbitrageOpportunity(
                        strategy=StrategyType.SPOT_ARB,
                        symbol=symbol,
                        profit_pct=net1,
                        buy_exchange="okx",
                        sell_exchange="htx",
                        buy_price=okx_ask,
                        sell_price=htx_bid,
                        spread_raw=spread1_raw,
                        estimated_fees=TOTAL_FEE_RATE,
                    ))

            # Direction 2: BUY on HTX, SELL on OKX
            if htx_ask > 0:
                spread2_raw = (okx_bid - htx_ask) / htx_ask * 100
                net2 = spread2_raw - fee_pct
                if net2 >= self.min_profit_pct:
                    results.append(SpotArbitrageOpportunity(
                        strategy=StrategyType.SPOT_ARB,
                        symbol=symbol,
                        profit_pct=net2,
                        buy_exchange="htx",
                        sell_exchange="okx",
                        buy_price=htx_ask,
                        sell_price=okx_bid,
                        spread_raw=spread2_raw,
                        estimated_fees=TOTAL_FEE_RATE,
                    ))

        results.sort(key=lambda x: x.profit_pct, reverse=True)
        return results

    def get_all_spreads(self) -> List[dict]:
        """
        Вернуть все спреды (включая ниже порога) для отображения в Scan.
        Возвращает список словарей, отсортированных по убыванию чистой прибыли.
        """
        items = []
        fee_pct = TOTAL_FEE_RATE * 100

        for symbol in self.common_pairs:
            okx = self.okx_prices.get(symbol)
            htx = self.htx_prices.get(symbol)
            if not okx or not htx:
                continue

            okx_ask, htx_bid = okx["ask"], htx["bid"]
            htx_ask, okx_bid = htx["ask"], okx["bid"]

            best_net = max(
                (htx_bid - okx_ask) / okx_ask * 100 - fee_pct if okx_ask > 0 else -999,
                (okx_bid - htx_ask) / htx_ask * 100 - fee_pct if htx_ask > 0 else -999,
            )

            if best_net > -999:
                items.append({"symbol": symbol, "net_profit_pct": best_net})

        items.sort(key=lambda x: x["net_profit_pct"], reverse=True)
        return items
