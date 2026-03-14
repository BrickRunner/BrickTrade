from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.fees import fee_bps_from_snapshot
from arbitrage.system.strategies.base import BaseStrategy


class SpotArbitrageStrategy(BaseStrategy):
    def __init__(self, min_edge_bps: float = 8.0, fee_bps: float = 4.0, slippage_buffer_bps: float = 2.0):
        super().__init__(StrategyId.SPOT_ARBITRAGE)
        self._min_edge_bps = min_edge_bps
        self._fee_bps = fee_bps
        self._slippage_buffer_bps = slippage_buffer_bps

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        if snapshot.spot_orderbooks:
            orderbooks = snapshot.spot_orderbooks
        else:
            return []
        exchanges = list(orderbooks.keys())
        if len(exchanges) < 2:
            return []

        best_intent: TradeIntent | None = None
        avg_spread_bps = snapshot.indicators.get("spread_bps", 0.0)
        dynamic_vol_buffer = min(12.0, snapshot.volatility * 40.0)
        dynamic_required = max(
            self._min_edge_bps,
            self._fee_bps + self._slippage_buffer_bps + max(0.0, avg_spread_bps * 0.35) + dynamic_vol_buffer,
        )
        for long_ex in exchanges:
            long_ask = orderbooks[long_ex].ask
            for short_ex in exchanges:
                if short_ex == long_ex:
                    continue
                short_bid = orderbooks[short_ex].bid
                edge_bps = (short_bid - long_ask) / long_ask * 10_000
                fees = (
                    fee_bps_from_snapshot(snapshot, long_ex, "spot", snapshot.symbol)
                    + fee_bps_from_snapshot(snapshot, short_ex, "spot", snapshot.symbol)
                )
                net_edge = edge_bps - fees
                if net_edge <= dynamic_required:
                    continue
                confidence = min(1.0, (edge_bps - dynamic_required) / max(dynamic_required, 1e-9))
                intent = TradeIntent(
                    strategy_id=self.strategy_id,
                    symbol=snapshot.symbol,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    side="market_neutral",
                    confidence=confidence,
                    expected_edge_bps=net_edge,
                    stop_loss_bps=max(dynamic_required, edge_bps * 0.55),
                    metadata={
                        "long_price": long_ask,
                        "short_price": short_bid,
                        "entry_mid": (long_ask + short_bid) / 2,
                        "take_profit_usd": 0.10,
                        "stop_loss_usd": 0.14,
                        "max_holding_seconds": 900.0,
                        "close_edge_bps": 0.4,
                        "leg_kinds": {long_ex: "spot", short_ex: "spot"},
                    },
                )
                if best_intent is None or intent.expected_edge_bps > best_intent.expected_edge_bps:
                    best_intent = intent
        return [best_intent] if best_intent else []
