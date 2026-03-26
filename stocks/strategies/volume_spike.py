from __future__ import annotations

import logging
from typing import List

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


class VolumeSpikeStrategy(StockBaseStrategy):
    """Abnormal-volume scalping strategy.

    Entry:
        - Volume spike ratio > ``threshold`` (default 2×).
        - Direction from CVD (cumulative volume delta).
        - VWAP filter: buy only below VWAP, sell only above.
    Exit:
        - Tight TP/SL for quick scalp.
    """

    def __init__(
        self,
        volume_threshold: float = 2.0,
        take_profit_pct: float = 1.5,
        stop_loss_pct: float = 1.0,
        quantity_lots: int = 1,
    ) -> None:
        super().__init__(StockStrategyId.VOLUME_SPIKE)
        self._vol_threshold = volume_threshold
        self._tp_pct = take_profit_pct
        self._sl_pct = stop_loss_pct
        self._quantity_lots = quantity_lots

    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        ind = snapshot.indicators
        vol_spike = ind.get("vol_spike", 0.0)
        cvd = ind.get("cvd_20", 0.0)
        vwap_val = ind.get("vwap_20", 0.0)
        price = snapshot.quote.last

        if price <= 0 or vol_spike < self._vol_threshold:
            return []

        # Direction from CVD — require meaningful directional volume.
        if cvd > 0.10:
            side = "buy"
        elif cvd < -0.10:
            side = "sell"
        else:
            return []

        # VWAP filter: buy below VWAP, sell above.
        if vwap_val > 0:
            if side == "buy" and price > vwap_val:
                return []
            if side == "sell" and price < vwap_val:
                return []

        confidence = min(1.0, 0.3 + (vol_spike - self._vol_threshold) / (self._vol_threshold * 1.5))

        return [
            StockTradeIntent(
                strategy_id=self._strategy_id,
                ticker=snapshot.ticker,
                side=side,
                quantity_lots=self._quantity_lots,
                confidence=confidence,
                expected_edge_pct=self._tp_pct * 0.6,
                stop_loss_pct=self._sl_pct,
                take_profit_pct=self._tp_pct,
                mode=self.default_mode,
                metadata={
                    "vol_spike": round(vol_spike, 2),
                    "cvd": round(cvd, 3),
                    "vwap": round(vwap_val, 4),
                },
            )
        ]
