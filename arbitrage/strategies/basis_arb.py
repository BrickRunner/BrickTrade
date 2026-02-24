"""
Стратегия 4: Basis Arbitrage (Фьючерсы vs Спот)

Суть: Игра на разнице цены фьючерса и спота.
Примеры:
  Cash & Carry: futures > spot
    → Покупаем BTC спот + SHORT BTC perpetual
    → Ждём схождения (фьючерс приближается к споту)
    → Профит = basis - fees

  Reverse Cash & Carry: futures < spot
    → Продаём BTC спот (или шортим) + LONG BTC perpetual
    → Профит = |basis| - fees

Формула: Basis = (futures_price - spot_price) / spot_price × 100
"""
import asyncio
from typing import Dict, List, Optional, Set, Tuple

from arbitrage.utils import get_arbitrage_logger
from arbitrage.strategies.base import BasisArbitrageOpportunity, StrategyType

logger = get_arbitrage_logger("basis_arb")

# Комиссии: спот taker 0.1% + перп taker 0.06% = 0.16% за открытие
# Итого round-trip ≈ 0.32%
ROUND_TRIP_FEE_PCT = 0.32

# Минимальный базис для входа (после комиссий должно что-то остаться)
MIN_BASIS_PCT = 0.15


def okx_inst_id(symbol: str) -> str:
    """BTCUSDT → BTC-USDT-SWAP"""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT-SWAP"
    return symbol


def okx_spot_id(symbol: str) -> str:
    """BTCUSDT → BTC-USDT"""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT"
    return symbol


class BasisArbitrageMonitor:
    """
    Мониторинг Basis арбитража.

    Для каждой пары сравниваем:
    - Спотовую цену (spot_mid = (bid + ask) / 2)
    - Цену перп-фьючерса (perp_mid)
    Basis = (perp - spot) / spot * 100

    Поддерживаемые комбинации:
    - OKX спот + OKX фьючерс
    - HTX спот + HTX фьючерс
    - OKX спот + HTX фьючерс (межбиржевой basis)
    - HTX спот + OKX фьючерс
    """

    def __init__(self, okx_client, htx_client, min_basis_pct: float = MIN_BASIS_PCT):
        self.okx_client = okx_client
        self.htx_client = htx_client
        self.min_basis_pct = min_basis_pct

        # Спотовые цены {symbol: mid_price}
        self.okx_spot: Dict[str, float] = {}
        self.htx_spot: Dict[str, float] = {}

        # Фьючерсные цены {symbol: mid_price}
        self.okx_perp: Dict[str, float] = {}
        self.htx_perp: Dict[str, float] = {}

        # Общие пары
        self.common_pairs: Set[str] = set()

    async def initialize(self) -> None:
        """Инициализация: получение пар"""
        try:
            okx_perp_res, htx_perp_res = await asyncio.gather(
                self.okx_client.get_instruments(inst_type="SWAP"),
                self.htx_client.get_instruments(),
                return_exceptions=True
            )

            okx_perp_syms: Set[str] = set()
            if not isinstance(okx_perp_res, Exception) and okx_perp_res.get("code") == "0":
                for inst in okx_perp_res.get("data", []):
                    inst_id = inst.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        okx_perp_syms.add(sym)

            # HTX instruments: data[] with contract_code like "BTC-USDT"
            htx_perp_syms: Set[str] = set()
            if not isinstance(htx_perp_res, Exception) and htx_perp_res.get("status") == "ok":
                for inst in htx_perp_res.get("data", []):
                    cc = inst.get("contract_code", "")
                    if cc.endswith("-USDT"):
                        sym = cc.replace("-", "")  # BTC-USDT → BTCUSDT
                        htx_perp_syms.add(sym)

            self.common_pairs = okx_perp_syms & htx_perp_syms
            logger.info(f"Basis arb: monitoring {len(self.common_pairs)} pairs")

        except Exception as e:
            logger.error(f"BasisArbitrage init error: {e}", exc_info=True)
            self.common_pairs = {
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"
            }

    async def update_prices(self) -> None:
        """Обновить спот и фьючерсные цены"""
        try:
            results = await asyncio.gather(
                self.okx_client.get_spot_tickers(),
                self.htx_client.get_spot_tickers(),
                self.okx_client.get_tickers(inst_type="SWAP"),
                self.htx_client.get_tickers(),
                return_exceptions=True
            )
            okx_spot_r, htx_spot_r, okx_perp_r, htx_perp_r = results

            # OKX спот
            if not isinstance(okx_spot_r, Exception) and okx_spot_r.get("code") == "0":
                for t in okx_spot_r.get("data", []):
                    inst_id = t.get("instId", "")
                    if not inst_id.endswith("-USDT"):
                        continue
                    sym = inst_id.replace("-", "")
                    try:
                        bid = float(t.get("bidPx") or 0)
                        ask = float(t.get("askPx") or 0)
                        if bid > 0 and ask > 0:
                            self.okx_spot[sym] = (bid + ask) / 2
                    except (ValueError, TypeError):
                        pass

            # HTX спот: data[] with symbol (lowercase), bid, ask
            if not isinstance(htx_spot_r, Exception) and htx_spot_r.get("status") == "ok":
                for t in htx_spot_r.get("data", []):
                    sym = t.get("symbol", "").upper()  # "btcusdt" → "BTCUSDT"
                    try:
                        bid = float(t.get("bid") or 0)
                        ask = float(t.get("ask") or 0)
                        if bid > 0 and ask > 0:
                            self.htx_spot[sym] = (bid + ask) / 2
                    except (ValueError, TypeError):
                        pass

            # OKX перп
            if not isinstance(okx_perp_r, Exception) and okx_perp_r.get("code") == "0":
                for t in okx_perp_r.get("data", []):
                    inst_id = t.get("instId", "")
                    if "-USDT-SWAP" not in inst_id:
                        continue
                    sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                    try:
                        bid = float(t.get("bidPx") or 0)
                        ask = float(t.get("askPx") or 0)
                        if bid > 0 and ask > 0:
                            self.okx_perp[sym] = (bid + ask) / 2
                    except (ValueError, TypeError):
                        pass

            # HTX перп: ticks[] with contract_code, bid[0], ask[0]
            if not isinstance(htx_perp_r, Exception) and htx_perp_r.get("status") == "ok":
                for t in htx_perp_r.get("ticks", []):
                    cc = t.get("contract_code", "")
                    if not cc.endswith("-USDT"):
                        continue
                    sym = cc.replace("-", "")  # BTC-USDT → BTCUSDT
                    try:
                        bid_data = t.get("bid") or []
                        ask_data = t.get("ask") or []
                        bid = float(bid_data[0]) if bid_data else 0.0
                        ask = float(ask_data[0]) if ask_data else 0.0
                        if bid > 0 and ask > 0:
                            self.htx_perp[sym] = (bid + ask) / 2
                    except (ValueError, TypeError, IndexError):
                        pass

        except Exception as e:
            logger.error(f"BasisArbitrage update error: {e}", exc_info=True)

    def _calc_basis(
        self,
        sym: str,
        spot_px: float,
        perp_px: float,
        spot_exchange: str,
        futures_exchange: str,
    ) -> Optional[BasisArbitrageOpportunity]:
        if spot_px <= 0 or perp_px <= 0:
            return None
        basis = (perp_px - spot_px) / spot_px * 100
        abs_basis = abs(basis)
        if abs_basis < self.min_basis_pct:
            return None

        if basis > 0:
            direction = "cash_and_carry"   # Futures premium → Buy spot, Short futures
        else:
            direction = "reverse_cash_carry"  # Futures discount → Short spot, Long futures

        return BasisArbitrageOpportunity(
            strategy=StrategyType.BASIS_ARB,
            symbol=sym,
            profit_pct=abs_basis,
            spot_exchange=spot_exchange,
            futures_exchange=futures_exchange,
            spot_price=spot_px,
            futures_price=perp_px,
            basis_pct=basis,
            direction=direction,
        )

    def calculate_opportunities(self) -> List[BasisArbitrageOpportunity]:
        """
        Найти basis арбитражные возможности во всех комбинациях:
        - OKX spot + OKX perp
        - HTX spot + HTX perp
        - OKX spot + HTX perp
        - HTX spot + OKX perp
        """
        results: List[BasisArbitrageOpportunity] = []

        # Получаем все символы со спотовыми ценами
        all_syms = set(self.okx_spot) | set(self.htx_spot)

        for sym in all_syms:
            combinations = [
                (self.okx_spot.get(sym, 0), self.okx_perp.get(sym, 0), "okx", "okx"),
                (self.htx_spot.get(sym, 0), self.htx_perp.get(sym, 0), "htx", "htx"),
                (self.okx_spot.get(sym, 0), self.htx_perp.get(sym, 0), "okx", "htx"),
                (self.htx_spot.get(sym, 0), self.okx_perp.get(sym, 0), "htx", "okx"),
            ]
            for spot_px, perp_px, spot_ex, perp_ex in combinations:
                opp = self._calc_basis(sym, spot_px, perp_px, spot_ex, perp_ex)
                if opp:
                    results.append(opp)

        # Убираем дубли: для каждого символа оставляем наибольший basis
        best: Dict[str, BasisArbitrageOpportunity] = {}
        for o in results:
            key = f"{o.symbol}_{o.spot_exchange}_{o.futures_exchange}"
            if key not in best or o.profit_pct > best[key].profit_pct:
                best[key] = o

        final = sorted(best.values(), key=lambda x: x.profit_pct, reverse=True)
        return final

    def get_all_spreads(self) -> List[dict]:
        """Все basis значения для Scan."""
        items = []
        all_syms = set(self.okx_spot) | set(self.htx_spot)
        for sym in all_syms:
            for spot_px, perp_px, spot_ex, perp_ex in [
                (self.okx_spot.get(sym, 0), self.okx_perp.get(sym, 0), "okx", "okx"),
                (self.htx_spot.get(sym, 0), self.htx_perp.get(sym, 0), "htx", "htx"),
            ]:
                if spot_px > 0 and perp_px > 0:
                    basis = (perp_px - spot_px) / spot_px * 100
                    items.append({
                        "symbol": sym,
                        "basis_pct": basis,
                        "spot_exchange": spot_ex,
                        "futures_exchange": perp_ex,
                    })
        items.sort(key=lambda x: abs(x["basis_pct"]), reverse=True)
        return items
