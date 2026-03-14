from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy


class FundingSpreadStrategy(BaseStrategy):
    def __init__(self, threshold_bps: float = 2.5):
        super().__init__(StrategyId.FUNDING_SPREAD)
        self._threshold_bps = threshold_bps

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        exchanges = list(snapshot.funding_rates.keys())
        if len(exchanges) < 2:
            return []
        # Funding spread mean-reversion mode: require calmer market than pure funding carry.
        if snapshot.volatility > 2.0:
            return []
        if abs(snapshot.trend_strength) > 5.0:
            return []

        best_intent: TradeIntent | None = None
        basis_abs = abs(snapshot.indicators.get("basis_bps", 0.0))
        dynamic_threshold = self._threshold_bps + min(6.0, basis_abs * 0.08)
        for a in exchanges:
            for b in exchanges:
                if a == b:
                    continue
                spread_bps = (snapshot.funding_rates[a] - snapshot.funding_rates[b]) * 10_000
                if spread_bps <= dynamic_threshold:
                    continue
                confidence = min(1.0, (spread_bps - dynamic_threshold) / max(dynamic_threshold, 1e-9))
                intent = TradeIntent(
                    strategy_id=self.strategy_id,
                    symbol=snapshot.symbol,
                    long_exchange=b,
                    short_exchange=a,
                    side="neutral_funding_spread",
                    confidence=confidence,
                    expected_edge_bps=spread_bps,
                    stop_loss_bps=max(self._threshold_bps, dynamic_threshold * 0.9),
                    metadata={
                        "funding_spread_bps": spread_bps,
                        "basis_bps": snapshot.indicators.get("basis_bps", 0.0),
                        "take_profit_usd": 0.08,
                        "stop_loss_usd": 0.12,
                        "max_holding_seconds": 1800.0,
                        "close_edge_bps": 0.45,
                    },
                )
                if best_intent is None or intent.expected_edge_bps > best_intent.expected_edge_bps:
                    best_intent = intent
        return [best_intent] if best_intent else []
