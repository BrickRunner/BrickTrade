from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.fees import fee_bps_from_snapshot
from arbitrage.system.strategies.base import BaseStrategy


class OrderbookImbalanceStrategy(BaseStrategy):
    def __init__(self, imbalance_ratio: float = 1.8, min_edge_bps: float = 2.0):
        super().__init__(StrategyId.ORDERBOOK_IMBALANCE)
        self._imbalance_ratio = imbalance_ratio
        self._min_edge_bps = min_edge_bps

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        if len(snapshot.orderbook_depth) < 1:
            return []
        exchanges = list(snapshot.orderbooks.keys())
        if len(exchanges) < 2:
            return []

        intents: List[TradeIntent] = []
        for ex in exchanges:
            depth = snapshot.orderbook_depth.get(ex)
            if not depth:
                continue
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            bid_vol = sum(float(q) for _, q in bids[:5])
            ask_vol = sum(float(q) for _, q in asks[:5])
            if bid_vol <= 0 or ask_vol <= 0:
                continue
            imbalance = bid_vol / ask_vol
            if imbalance < self._imbalance_ratio and (1 / imbalance) < self._imbalance_ratio:
                continue

            # Directional bias: if bid pressure >> ask, go long on this exchange, hedge short elsewhere.
            if imbalance >= self._imbalance_ratio:
                long_ex = ex
                short_ex = max((e for e in exchanges if e != ex), key=lambda e: snapshot.orderbooks[e].bid)
                long_price = snapshot.orderbooks[long_ex].ask
                short_price = snapshot.orderbooks[short_ex].bid
                edge_bps = (short_price - long_price) / max(long_price, 1e-9) * 10_000
                fees = (
                    fee_bps_from_snapshot(snapshot, long_ex, "perp", snapshot.symbol)
                    + fee_bps_from_snapshot(snapshot, short_ex, "perp", snapshot.symbol)
                )
                net_edge = edge_bps - fees
                if net_edge < self._min_edge_bps:
                    continue
                intents.append(
                    TradeIntent(
                        strategy_id=self.strategy_id,
                        symbol=snapshot.symbol,
                        long_exchange=long_ex,
                        short_exchange=short_ex,
                        side="imbalance_long",
                        confidence=min(1.0, imbalance / (self._imbalance_ratio * 2)),
                        expected_edge_bps=net_edge,
                        stop_loss_bps=max(self._min_edge_bps, edge_bps * 0.7),
                        metadata={
                            "imbalance": imbalance,
                            "long_price": long_price,
                            "short_price": short_price,
                            "entry_mid": (long_price + short_price) / 2,
                            "leg_kinds": {long_ex: "perp", short_ex: "perp"},
                            "limit_prices": {"buy": long_price, "sell": short_price},
                            "take_profit_usd": 0.09,
                            "stop_loss_usd": 0.13,
                            "max_holding_seconds": 600.0,
                            "close_edge_bps": 0.4,
                        },
                    )
                )
            else:
                # Ask pressure >> bid => short bias on this exchange, hedge long elsewhere.
                short_ex = ex
                long_ex = min((e for e in exchanges if e != ex), key=lambda e: snapshot.orderbooks[e].ask)
                long_price = snapshot.orderbooks[long_ex].ask
                short_price = snapshot.orderbooks[short_ex].bid
                edge_bps = (short_price - long_price) / max(long_price, 1e-9) * 10_000
                fees = (
                    fee_bps_from_snapshot(snapshot, long_ex, "perp", snapshot.symbol)
                    + fee_bps_from_snapshot(snapshot, short_ex, "perp", snapshot.symbol)
                )
                net_edge = edge_bps - fees
                if net_edge < self._min_edge_bps:
                    continue
                intents.append(
                    TradeIntent(
                        strategy_id=self.strategy_id,
                        symbol=snapshot.symbol,
                        long_exchange=long_ex,
                        short_exchange=short_ex,
                        side="imbalance_short",
                        confidence=min(1.0, (1 / imbalance) / (self._imbalance_ratio * 2)),
                        expected_edge_bps=net_edge,
                        stop_loss_bps=max(self._min_edge_bps, edge_bps * 0.7),
                        metadata={
                            "imbalance": imbalance,
                            "long_price": long_price,
                            "short_price": short_price,
                            "entry_mid": (long_price + short_price) / 2,
                            "leg_kinds": {long_ex: "perp", short_ex: "perp"},
                            "limit_prices": {"buy": long_price, "sell": short_price},
                            "take_profit_usd": 0.09,
                            "stop_loss_usd": 0.13,
                            "max_holding_seconds": 600.0,
                            "close_edge_bps": 0.4,
                        },
                    )
                )
        return intents
