from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Dict, List

from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot


@dataclass
class SyntheticMarketDataProvider:
    exchanges: List[str]

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        base = 40_000.0 if symbol.startswith("BTC") else 2_500.0
        drift = random.uniform(-0.002, 0.002)
        mid = base * (1 + drift)
        orderbooks: Dict[str, OrderBookSnapshot] = {}
        spot_orderbooks: Dict[str, OrderBookSnapshot] = {}
        orderbook_depth: Dict[str, Dict[str, list]] = {}
        spot_orderbook_depth: Dict[str, Dict[str, list]] = {}
        balances: Dict[str, float] = {}
        fee_bps: Dict[str, Dict[str, float]] = {}
        for i, exchange in enumerate(self.exchanges):
            spread = (3 + i) / 10_000
            local_mid = mid * (1 + random.uniform(-0.0007, 0.0007))
            orderbooks[exchange] = OrderBookSnapshot(
                exchange=exchange,
                symbol=symbol,
                bid=local_mid * (1 - spread),
                ask=local_mid * (1 + spread),
                timestamp=time.time(),
            )
            spot_orderbooks[exchange] = OrderBookSnapshot(
                exchange=exchange,
                symbol=symbol,
                bid=local_mid * (1 - spread * 1.2),
                ask=local_mid * (1 + spread * 1.2),
                timestamp=time.time(),
            )
            orderbook_depth[exchange] = {
                "bids": [[orderbooks[exchange].bid, 10.0], [orderbooks[exchange].bid * 0.999, 8.0]],
                "asks": [[orderbooks[exchange].ask, 10.0], [orderbooks[exchange].ask * 1.001, 8.0]],
                "timestamp": time.time(),
            }
            spot_orderbook_depth[exchange] = {
                "bids": [[spot_orderbooks[exchange].bid, 10.0], [spot_orderbooks[exchange].bid * 0.999, 8.0]],
                "asks": [[spot_orderbooks[exchange].ask, 10.0], [spot_orderbooks[exchange].ask * 1.001, 8.0]],
                "timestamp": time.time(),
            }
            balances[exchange] = 1000.0
            fee_bps[exchange] = {"spot": 6.0, "perp": 6.0}

        funding = {exchange: random.uniform(-0.0002, 0.0002) for exchange in self.exchanges}
        indicators = {
            "rsi": random.uniform(20, 80),
            "ema_fast": mid * 0.998,
            "ema_slow": mid * 1.001,
            "vwap": mid,
            "bb_upper": mid * 1.01,
            "bb_lower": mid * 0.99,
            "spot_price": mid * 0.999,
            "perp_price": mid * 1.001,
        }
        return MarketSnapshot(
            symbol=symbol,
            orderbooks=orderbooks,
            spot_orderbooks=spot_orderbooks,
            orderbook_depth=orderbook_depth,
            spot_orderbook_depth=spot_orderbook_depth,
            balances=balances,
            fee_bps=fee_bps,
            funding_rates=funding,
            volatility=0.2,
            trend_strength=0.3,
            atr=40.0,
            atr_rolling=50.0,
            indicators=indicators,
        )

    async def health(self) -> Dict[str, float]:
        return {exchange: random.uniform(10.0, 100.0) for exchange in self.exchanges}
