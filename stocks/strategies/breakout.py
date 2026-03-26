from __future__ import annotations

import logging
from typing import List

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


class BreakoutStrategy(StockBaseStrategy):
    """Support/resistance breakout with volume confirmation.

    Entry:
        - Price breaks above recent swing high (or below swing low).
        - Volume spike > ``volume_multiplier`` × 20-bar average.
    Stop-loss:
        - ATR × ``atr_multiplier`` below (above) breakout level.
    """

    def __init__(
        self,
        lookback: int = 20,
        volume_multiplier: float = 1.5,
        atr_multiplier: float = 1.5,
        quantity_lots: int = 1,
    ) -> None:
        super().__init__(StockStrategyId.BREAKOUT)
        self._lookback = lookback
        self._vol_mult = volume_multiplier
        self._atr_mult = atr_multiplier
        self._quantity_lots = quantity_lots

    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        if len(snapshot.candles) < self._lookback + 1:
            return []

        ind = snapshot.indicators
        vol_spike = ind.get("vol_spike", 0.0)
        atr_val = ind.get("atr_14", 0.0)
        price = snapshot.quote.last

        if price <= 0 or atr_val <= 0:
            return []

        # Volume confirmation gate.
        if vol_spike < self._vol_mult:
            return []

        # Swing high / low from last ``lookback`` candles (excluding current).
        recent = snapshot.candles[-(self._lookback + 1):-1]
        swing_high = max(c.high for c in recent)
        swing_low = min(c.low for c in recent)

        side = None
        if price > swing_high:
            side = "buy"
            sl_pct = (atr_val * self._atr_mult / price) * 100
            tp_pct = sl_pct * 2.0
        elif price < swing_low:
            side = "sell"
            sl_pct = (atr_val * self._atr_mult / price) * 100
            tp_pct = sl_pct * 2.0
        else:
            return []

        confidence = min(1.0, 0.3 + (vol_spike - self._vol_mult) / self._vol_mult)

        return [
            StockTradeIntent(
                strategy_id=self._strategy_id,
                ticker=snapshot.ticker,
                side=side,
                quantity_lots=self._quantity_lots,
                confidence=confidence,
                expected_edge_pct=tp_pct * 0.5,
                stop_loss_pct=round(sl_pct, 2),
                take_profit_pct=round(tp_pct, 2),
                mode=self.default_mode,
                metadata={
                    "swing_high": round(swing_high, 4),
                    "swing_low": round(swing_low, 4),
                    "vol_spike": round(vol_spike, 2),
                },
            )
        ]
