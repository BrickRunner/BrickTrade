"""
Стратегия 2: Фьючерсный межбиржевой арбитраж (Cross-Exchange Futures Arbitrage)

Суть: Открываем противоположные позиции на двух биржах по перп-фьючерсам.
Пример: SHORT HTX + LONG OKX если HTX дороже, ждём схождения цены.
Формула: Spread = (Price_short - Price_long) / Price_long * 100
"""
import asyncio
from typing import Dict, List, Set, Tuple

from arbitrage.utils import get_arbitrage_logger, calculate_spread
from arbitrage.strategies.base import FuturesArbitrageOpportunity, StrategyType

logger = get_arbitrage_logger("futures_arb")

# Комиссии: OKX 0.06% taker + HTX 0.04% taker = 0.10% за одну сторону
# Round-trip entry+exit ≈ 0.20%
TOTAL_FEE_PCT = 0.20


class FuturesArbitrageMonitor:
    """
    Мониторинг фьючерсного арбитража между OKX и HTX.
    Использует перп-фьючерсы (SWAP / linear).
    """

    def __init__(self, okx_client, htx_client, min_spread_pct: float = 0.05):
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.min_spread_pct = min_spread_pct

        self.okx_prices: Dict[str, Dict[str, float]] = {}
        self.htx_prices: Dict[str, Dict[str, float]] = {}
        self.monitored_pairs: Set[str] = set()

    async def initialize(self) -> None:
        """Инициализация: получение общих фьючерсных пар"""
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_instruments(inst_type="SWAP"),
                self.htx_client.get_instruments(),
                return_exceptions=True
            )

            okx_syms: Set[str] = set()
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for inst in okx_result.get("data", []):
                    inst_id = inst.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        okx_syms.add(sym)

            # HTX instruments: data[] with contract_code like "BTC-USDT"
            htx_syms: Set[str] = set()
            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for inst in htx_result.get("data", []):
                    cc = inst.get("contract_code", "")
                    if cc.endswith("-USDT"):
                        sym = cc.replace("-", "")  # BTC-USDT → BTCUSDT
                        htx_syms.add(sym)

            self.monitored_pairs = okx_syms & htx_syms
            logger.info(f"Futures arb: monitoring {len(self.monitored_pairs)} common pairs")

        except Exception as e:
            logger.error(f"FuturesArbitrage init error: {e}", exc_info=True)
            self.monitored_pairs = {
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"
            }

    async def update_prices(self) -> Tuple[int, int]:
        """Обновить цены фьючерсов. Возвращает (okx_count, htx_count)."""
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_tickers(inst_type="SWAP"),
                self.htx_client.get_tickers(),
                return_exceptions=True
            )

            okx_count = 0
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for t in okx_result.get("data", []):
                    inst_id = t.get("instId", "")
                    if "-USDT-SWAP" not in inst_id:
                        continue
                    sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                    if sym not in self.monitored_pairs:
                        continue
                    try:
                        bid = float(t.get("bidPx") or 0)
                        ask = float(t.get("askPx") or 0)
                        if bid > 0 and ask > 0:
                            self.okx_prices[sym] = {"bid": bid, "ask": ask}
                            okx_count += 1
                    except (ValueError, TypeError):
                        pass

            # HTX futures tickers: ticks[] with contract_code, bid[0], ask[0]
            htx_count = 0
            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for t in htx_result.get("ticks", []):
                    cc = t.get("contract_code", "")
                    if not cc.endswith("-USDT"):
                        continue
                    sym = cc.replace("-", "")  # BTC-USDT → BTCUSDT
                    if sym not in self.monitored_pairs:
                        continue
                    try:
                        bid_data = t.get("bid") or []
                        ask_data = t.get("ask") or []
                        bid = float(bid_data[0]) if bid_data else 0.0
                        ask = float(ask_data[0]) if ask_data else 0.0
                        if bid > 0 and ask > 0:
                            self.htx_prices[sym] = {"bid": bid, "ask": ask}
                            htx_count += 1
                    except (ValueError, TypeError, IndexError):
                        pass

            return okx_count, htx_count

        except Exception as e:
            logger.error(f"FuturesArbitrage update_prices error: {e}", exc_info=True)
            return 0, 0

    def calculate_opportunities(self) -> List[FuturesArbitrageOpportunity]:
        """
        Рассчитать фьючерсные арбитражные возможности.

        Dir1 (okx_long): BUY OKX @ okx_ask, SELL HTX @ htx_bid
                         spread = (htx_bid - okx_ask) / okx_ask * 100
        Dir2 (htx_long): BUY HTX @ htx_ask, SELL OKX @ okx_bid
                         spread = (okx_bid - htx_ask) / htx_ask * 100
        """
        results: List[FuturesArbitrageOpportunity] = []

        for sym in self.monitored_pairs:
            okx = self.okx_prices.get(sym)
            htx = self.htx_prices.get(sym)
            if not okx or not htx:
                continue

            okx_bid, okx_ask = okx["bid"], okx["ask"]
            htx_bid, htx_ask = htx["bid"], htx["ask"]

            # Direction 1: LONG OKX, SHORT HTX
            spread1 = calculate_spread(htx_bid, okx_ask)
            # Direction 2: LONG HTX, SHORT OKX
            spread2 = calculate_spread(okx_bid, htx_ask)

            best_spread = max(spread1, spread2)
            if best_spread < self.min_spread_pct:
                continue

            if spread1 >= spread2:
                results.append(FuturesArbitrageOpportunity(
                    strategy=StrategyType.FUTURES_ARB,
                    symbol=sym,
                    profit_pct=spread1,
                    long_exchange="okx",
                    short_exchange="htx",
                    long_price=okx_ask,
                    short_price=htx_bid,
                    direction="okx_long",
                ))
            else:
                results.append(FuturesArbitrageOpportunity(
                    strategy=StrategyType.FUTURES_ARB,
                    symbol=sym,
                    profit_pct=spread2,
                    long_exchange="htx",
                    short_exchange="okx",
                    long_price=htx_ask,
                    short_price=okx_bid,
                    direction="htx_long",
                ))

        results.sort(key=lambda x: x.profit_pct, reverse=True)
        return results

    def get_all_spreads(self) -> List[dict]:
        """Все спреды для Scan (без фильтра порога)."""
        items = []
        for sym in self.monitored_pairs:
            okx = self.okx_prices.get(sym)
            htx = self.htx_prices.get(sym)
            if not okx or not htx:
                continue
            s1 = calculate_spread(htx["bid"], okx["ask"])
            s2 = calculate_spread(okx["bid"], htx["ask"])
            best = max(s1, s2)
            items.append({
                "symbol": sym,
                "spread_pct": best,
                "direction": "okx_long" if s1 >= s2 else "htx_long",
            })
        items.sort(key=lambda x: x["spread_pct"], reverse=True)
        return items
