"""
Стратегия 3: Funding Rate Arbitrage

Суть: Зарабатываем на разнице ставок финансирования.
Логика:
  - SHORT там, где funding ПОЛОЖИТЕЛЬНЫЙ (лонги платят шортам → нам платят)
  - LONG там, где funding ОТРИЦАТЕЛЬНЫЙ (шорты платят лонгам → нам платят)
  - Net profit = short_funding - long_funding (8ч интервал)

Формула: Profit = funding_short - funding_long - entry_fees
"""
import asyncio
from typing import Dict, List, Tuple

from arbitrage.utils import get_arbitrage_logger
from arbitrage.strategies.base import FundingArbitrageOpportunity, StrategyType

logger = get_arbitrage_logger("funding_arb")

# Типичные комиссии за вход+выход позиции (maker/taker mix)
ENTRY_FEE_PCT = 0.04   # 0.04% за открытие (2 ноги × 0.02%)
EXIT_FEE_PCT = 0.04    # 0.04% за закрытие
TOTAL_ROUND_TRIP_FEE = ENTRY_FEE_PCT + EXIT_FEE_PCT  # 0.08%

# Минимальное количество интервалов для окупаемости комиссий
MIN_INTERVALS_TO_PROFIT = 2  # позиция должна держаться хотя бы 2 интервала


def okx_inst_id(symbol: str) -> str:
    """BTCUSDT → BTC-USDT-SWAP"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    return symbol


class FundingArbitrageMonitor:
    """
    Мониторинг Funding Rate арбитража между OKX и HTX.

    Алгоритм:
    1. Получаем funding rates со всех торговых пар OKX и HTX
    2. Для каждой общей пары находим разницу funding rates
    3. Если разница > threshold → это возможность

    Примечание:
    - OKX: funding rate в поле 'fundingRate' в тикерах SWAP
      Интервал: каждые 8 часов (00:00, 08:00, 16:00 UTC)
    - HTX: funding rate в поле 'funding_rate' в /swap-api/v3/swap_batch_funding_rate
      Интервал: каждые 8 часов (00:00, 08:00, 16:00 UTC)
    - Ставки указаны как доля (не %). Например 0.0001 = 0.01%
    """

    def __init__(self, okx_client, htx_client, min_net_funding_pct: float = 0.02):
        self.okx_client = okx_client
        self.htx_client = htx_client
        # Минимальная суммарная ставка за 8ч после вычета комиссий
        self.min_net_funding_pct = min_net_funding_pct

        # Кэш ставок {symbol: funding_rate_float}  (уже в %)
        self.okx_funding: Dict[str, float] = {}
        self.htx_funding: Dict[str, float] = {}

        # Следующее время funding {symbol: timestamp_ms}
        self.okx_next_funding: Dict[str, int] = {}
        self.htx_next_funding: Dict[str, int] = {}

    async def update_funding_rates(self) -> Tuple[int, int]:
        """
        Обновить ставки финансирования с обеих бирж.
        OKX: тикеры SWAP содержат fundingRate
        HTX: /swap-api/v3/swap_batch_funding_rate
        """
        try:
            okx_result, htx_result = await asyncio.gather(
                self.okx_client.get_funding_rates_all(),
                self.htx_client.get_funding_rates(),
                return_exceptions=True
            )

            okx_count = 0
            if not isinstance(okx_result, Exception) and okx_result.get("code") == "0":
                for t in okx_result.get("data", []):
                    inst_id = t.get("instId", "")
                    if "-USDT-SWAP" not in inst_id:
                        continue
                    sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                    try:
                        rate_str = t.get("fundingRate") or t.get("nextFundingRate")
                        if rate_str is None:
                            continue
                        rate = float(rate_str) * 100  # конвертируем в %
                        self.okx_funding[sym] = rate
                        next_ts = t.get("nextFundingTime")
                        if next_ts:
                            self.okx_next_funding[sym] = int(next_ts)
                        okx_count += 1
                    except (ValueError, TypeError):
                        pass

            # HTX funding: data[] with contract_code, funding_rate (decimal string)
            htx_count = 0
            if not isinstance(htx_result, Exception) and htx_result.get("status") == "ok":
                for t in htx_result.get("data", []):
                    cc = t.get("contract_code", "")
                    if not cc.endswith("-USDT"):
                        continue
                    sym = cc.replace("-", "")  # BTC-USDT → BTCUSDT
                    try:
                        rate_str = t.get("funding_rate")
                        if rate_str is None:
                            continue
                        rate = float(rate_str) * 100  # конвертируем в %
                        self.htx_funding[sym] = rate
                        next_ts = t.get("settlement_time")
                        if next_ts:
                            self.htx_next_funding[sym] = int(next_ts)
                        htx_count += 1
                    except (ValueError, TypeError):
                        pass

            logger.debug(
                f"Funding rates updated: OKX={okx_count}, HTX={htx_count}"
            )
            return okx_count, htx_count

        except Exception as e:
            logger.error(f"FundingArbitrage update error: {e}", exc_info=True)
            return 0, 0

    def calculate_opportunities(self) -> List[FundingArbitrageOpportunity]:
        """
        Найти возможности для funding арбитража.

        Логика:
        - Если okx_funding > htx_funding:
            SHORT OKX (получаем okx_funding) + LONG HTX (платим htx_funding)
            net = okx_funding - htx_funding
        - Если htx_funding > okx_funding:
            SHORT HTX + LONG OKX
            net = htx_funding - okx_funding
        """
        results: List[FundingArbitrageOpportunity] = []

        common = set(self.okx_funding.keys()) & set(self.htx_funding.keys())

        for sym in common:
            okx_rate = self.okx_funding[sym]    # %
            htx_rate = self.htx_funding[sym]    # %

            diff = abs(okx_rate - htx_rate)

            if diff < self.min_net_funding_pct:
                continue

            if okx_rate > htx_rate:
                # SHORT OKX (высокий positive funding → лонги платят нам)
                # LONG HTX (низкий или negative funding)
                long_ex = "htx"
                short_ex = "okx"
                long_rate = htx_rate
                short_rate = okx_rate
            else:
                long_ex = "okx"
                short_ex = "htx"
                long_rate = okx_rate
                short_rate = htx_rate

            net_8h = short_rate - long_rate

            # Строим описание следующего funding time
            next_ts = self.htx_next_funding.get(sym) or self.okx_next_funding.get(sym)
            next_funding_str = None
            if next_ts:
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc)
                next_funding_str = dt.strftime("%H:%M UTC")

            results.append(FundingArbitrageOpportunity(
                strategy=StrategyType.FUNDING_ARB,
                symbol=sym,
                profit_pct=net_8h,
                long_exchange=long_ex,
                short_exchange=short_ex,
                long_funding_rate=long_rate,
                short_funding_rate=short_rate,
                net_funding_8h=net_8h,
                next_funding_time=next_funding_str,
            ))

        results.sort(key=lambda x: x.profit_pct, reverse=True)
        return results

    def get_all_spreads(self) -> List[dict]:
        """Все фандинг-спреды для отображения в Scan."""
        items = []
        common = set(self.okx_funding.keys()) & set(self.htx_funding.keys())
        for sym in common:
            diff = abs(self.okx_funding[sym] - self.htx_funding[sym])
            items.append({
                "symbol": sym,
                "okx_rate": self.okx_funding[sym],
                "htx_rate": self.htx_funding[sym],
                "diff_pct": diff,
            })
        items.sort(key=lambda x: x["diff_pct"], reverse=True)
        return items
