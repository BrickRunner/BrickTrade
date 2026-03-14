from __future__ import annotations

import time
from typing import Dict, List, Tuple

from arbitrage.core.market_data import MarketDataEngine
import os

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.fees import fee_bps_from_snapshot
from arbitrage.system.strategies.base import BaseStrategy


DEFAULT_TAKER_FEE_BPS = 6.0

TRI_PATHS: List[Dict] = [
    {
        "name": "USDT->BTC->ETH->USDT",
        "steps": [
            ("BTCUSDT", "buy"),
            ("ETHBTC", "buy"),
            ("ETHUSDT", "sell"),
        ],
    },
    {
        "name": "USDT->ETH->BTC->USDT",
        "steps": [
            ("ETHUSDT", "buy"),
            ("ETHBTC", "sell"),
            ("BTCUSDT", "sell"),
        ],
    },
    {
        "name": "USDT->BTC->BNB->USDT",
        "steps": [
            ("BTCUSDT", "buy"),
            ("BNBBTC", "buy"),
            ("BNBUSDT", "sell"),
        ],
    },
    {
        "name": "USDT->BNB->BTC->USDT",
        "steps": [
            ("BNBUSDT", "buy"),
            ("BNBBTC", "sell"),
            ("BTCUSDT", "sell"),
        ],
    },
]

MULTI_PATHS: List[Dict] = [
    {
        "name": "USDT->BTC->ETH->SOL->USDT",
        "steps": [
            ("BTCUSDT", "buy"),
            ("ETHBTC", "buy"),
            ("SOLETH", "buy"),
            ("SOLUSDT", "sell"),
        ],
    },
    {
        "name": "USDT->ETH->SOL->BNB->USDT",
        "steps": [
            ("ETHUSDT", "buy"),
            ("SOLETH", "buy"),
            ("SOLBNB", "sell"),
            ("BNBUSDT", "sell"),
        ],
    },
]


class TriangularArbitrageStrategy(BaseStrategy):
    def __init__(
        self,
        market_data: MarketDataEngine,
        exchanges: List[str],
        min_net_profit_pct: float = 0.05,
        refresh_seconds: float = 1.0,
        base_usdt: float = 10.0,
    ):
        super().__init__(StrategyId.TRIANGULAR_ARBITRAGE)
        self._market_data = market_data
        self._exchanges = exchanges
        self._min_net_profit_pct = min_net_profit_pct
        self._refresh_seconds = refresh_seconds
        self._base_usdt = base_usdt
        self._last_refresh = 0.0
        self._books: Dict[str, Dict[str, Tuple[float, float]]] = {}
        self._slip_bps = float(os.getenv("TRI_MAX_SLIPPAGE_BPS", "2.0"))
        self._require_atomic = os.getenv("TRI_REQUIRE_ATOMIC", "false").strip().lower() in {"1", "true", "yes", "on"}

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        await self._update_books()
        intents: List[TradeIntent] = []
        for exchange in self._exchanges:
            orderbook = self._books.get(exchange, {})
            for path in TRI_PATHS:
                if not all(pair in orderbook for pair, _ in path["steps"]):
                    continue
                if self._require_atomic:
                    # No native OCO/RFQ wired yet -> skip unless explicitly disabled.
                    continue
                fee_bps = fee_bps_from_snapshot(snapshot, exchange, "spot", snapshot.symbol) or DEFAULT_TAKER_FEE_BPS
                profit_pct, legs = self._calc_path_profit(path, orderbook, fee_bps)
                if profit_pct < self._min_net_profit_pct:
                    continue
                for leg in legs:
                    leg["exchange"] = exchange
                expected_bps = profit_pct * 100
                intents.append(
                    TradeIntent(
                        strategy_id=self.strategy_id,
                        symbol=path["name"],
                        long_exchange=exchange,
                        short_exchange=exchange,
                        side="triangular_spot",
                        confidence=min(1.0, expected_bps / max(self._min_net_profit_pct * 200, 1e-9)),
                        expected_edge_bps=expected_bps,
                        stop_loss_bps=max(5.0, expected_bps * 0.6),
                        metadata={
                            "legs": legs,
                            "base_usdt": self._base_usdt,
                            "exchange": exchange,
                            "notional_usd": self._base_usdt,
                        },
                    )
                )
        return intents

    async def _update_books(self) -> None:
        now = time.time()
        if now - self._last_refresh < self._refresh_seconds:
            return
        self._last_refresh = now
        required_pairs = {pair for path in TRI_PATHS for pair, _ in path["steps"]}
        for exchange in self._exchanges:
            book_map: Dict[str, Tuple[float, float]] = {}
            for pair in required_pairs:
                book = await self._market_data.fetch_spot_orderbook_depth(exchange, pair, levels=1)
                if not book:
                    continue
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if not bids or not asks:
                    continue
                bid = float(bids[0][0])
                ask = float(asks[0][0])
                if bid <= 0 or ask <= 0:
                    continue
                book_map[pair] = (bid, ask)
            self._books[exchange] = book_map

    def _calc_path_profit(self, path: Dict, orderbook: Dict[str, Tuple[float, float]], fee_bps: float) -> Tuple[float, List[Dict]]:
        amount = self._base_usdt
        legs: List[Dict] = []
        for pair, side in path["steps"]:
            bid, ask = orderbook[pair]
            if side == "buy":
                if ask <= 0:
                    return 0.0, []
                qty_base = amount / ask
                limit_price = ask * (1 + self._slip_bps / 10_000)
                legs.append({"symbol": pair, "side": "buy", "quantity_base": qty_base, "limit_price": limit_price})
                amount = qty_base
            else:
                if bid <= 0:
                    return 0.0, []
                qty_base = amount
                limit_price = bid * (1 - self._slip_bps / 10_000)
                legs.append({"symbol": pair, "side": "sell", "quantity_base": qty_base, "limit_price": limit_price})
                amount = amount * bid

        fee_multiplier = (1 - (fee_bps / 10_000)) ** len(path["steps"])
        final_amount = amount * fee_multiplier
        profit_pct = (final_amount - self._base_usdt) / self._base_usdt * 100
        return profit_pct, legs


class MultiTriangularArbitrageStrategy(TriangularArbitrageStrategy):
    def __init__(
        self,
        market_data: MarketDataEngine,
        exchanges: List[str],
        min_net_profit_pct: float = 0.08,
        refresh_seconds: float = 1.0,
        base_usdt: float = 10.0,
    ):
        super().__init__(market_data, exchanges, min_net_profit_pct, refresh_seconds, base_usdt)
        self._strategy_id = StrategyId.MULTI_TRIANGULAR_ARBITRAGE

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        await self._update_books_multi()
        intents: List[TradeIntent] = []
        for exchange in self._exchanges:
            orderbook = self._books.get(exchange, {})
            for path in MULTI_PATHS:
                if not all(pair in orderbook for pair, _ in path["steps"]):
                    continue
                if self._require_atomic:
                    continue
                fee_bps = fee_bps_from_snapshot(snapshot, exchange, "spot", snapshot.symbol) or DEFAULT_TAKER_FEE_BPS
                profit_pct, legs = self._calc_path_profit(path, orderbook, fee_bps)
                if profit_pct < self._min_net_profit_pct:
                    continue
                for leg in legs:
                    leg["exchange"] = exchange
                expected_bps = profit_pct * 100
                intents.append(
                    TradeIntent(
                        strategy_id=self.strategy_id,
                        symbol=path["name"],
                        long_exchange=exchange,
                        short_exchange=exchange,
                        side="multi_triangular_spot",
                        confidence=min(1.0, expected_bps / max(self._min_net_profit_pct * 200, 1e-9)),
                        expected_edge_bps=expected_bps,
                        stop_loss_bps=max(6.0, expected_bps * 0.6),
                        metadata={
                            "legs": legs,
                            "base_usdt": self._base_usdt,
                            "exchange": exchange,
                            "notional_usd": self._base_usdt,
                        },
                    )
                )
        return intents

    async def _update_books_multi(self) -> None:
        now = time.time()
        if now - self._last_refresh < self._refresh_seconds:
            return
        self._last_refresh = now
        required_pairs = {pair for path in MULTI_PATHS for pair, _ in path["steps"]}
        for exchange in self._exchanges:
            book_map: Dict[str, Tuple[float, float]] = {}
            for pair in required_pairs:
                book = await self._market_data.fetch_spot_orderbook_depth(exchange, pair, levels=1)
                if not book:
                    continue
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if not bids or not asks:
                    continue
                bid = float(bids[0][0])
                ask = float(asks[0][0])
                if bid <= 0 or ask <= 0:
                    continue
                book_map[pair] = (bid, ask)
            self._books[exchange] = book_map
