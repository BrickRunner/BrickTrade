from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy


class GridStrategy(BaseStrategy):
    def __init__(self, max_breakout_ratio: float = 1.2):
        super().__init__(StrategyId.GRID)
        self._max_breakout_ratio = max_breakout_ratio

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        if snapshot.atr_rolling <= 0:
            return []
        atr_ratio = snapshot.atr / max(snapshot.atr_rolling, 1e-9)
        if atr_ratio >= 1.0:
            return []
        if atr_ratio > self._max_breakout_ratio:
            return []
        if abs(snapshot.trend_strength) > 5.0:
            return []

        exchanges = list(snapshot.orderbooks.keys())
        if len(exchanges) < 2:
            return []
        ref = snapshot.orderbooks[exchanges[0]].mid
        anchor = snapshot.orderbooks[exchanges[1]].mid
        edge_bps = abs(ref - anchor) / max(ref, 1e-9) * 10_000
        rsi = snapshot.indicators.get("rsi", 50.0)
        bb_upper = snapshot.indicators.get("bb_upper", ref * 1.01)
        bb_lower = snapshot.indicators.get("bb_lower", ref * 0.99)
        bb_width_bps = ((bb_upper - bb_lower) / max(ref, 1e-9)) * 10_000
        if edge_bps < 2.0 or bb_width_bps < 15.0:
            return []
        # Grid requires non-directional momentum.
        if rsi < 30.0 or rsi > 70.0:
            return []
        return [
            TradeIntent(
                strategy_id=self.strategy_id,
                symbol=snapshot.symbol,
                long_exchange=exchanges[0],
                short_exchange=exchanges[1],
                side="grid_reversion",
                confidence=min(0.85, 0.5 + edge_bps / 100.0),
                expected_edge_bps=edge_bps,
                stop_loss_bps=max(10.0, edge_bps * 0.75),
                metadata={
                    "atr": snapshot.atr,
                    "atr_rolling": snapshot.atr_rolling,
                    "entry_mid": (ref + anchor) / 2,
                    "grid_levels": 6.0,
                    "grid_step_bps": max(2.0, edge_bps / 4.0),
                    "take_profit_usd": 0.07,
                    "stop_loss_usd": 0.11,
                    "max_holding_seconds": 1200.0,
                    "close_edge_bps": 0.35,
                },
            )
        ]
