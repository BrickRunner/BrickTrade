from __future__ import annotations

import logging
from typing import List

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


class TrendFollowingStrategy(StockBaseStrategy):
    """EMA crossover with ADX trend-strength confirmation.

    Entry:
        - EMA(fast) crosses above EMA(slow) + ADX > threshold → buy
        - EMA(fast) crosses below EMA(slow) + ADX > threshold → sell
    Exit:
        - Trailing stop via ATR multiplier.
    """

    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        adx_threshold: float = 25.0,
        atr_sl_mult: float = 2.0,
        quantity_lots: int = 1,
    ) -> None:
        super().__init__(StockStrategyId.TREND_FOLLOWING)
        self._ema_fast_period = ema_fast
        self._ema_slow_period = ema_slow
        self._adx_threshold = adx_threshold
        self._atr_sl_mult = atr_sl_mult
        self._quantity_lots = quantity_lots
        # Track previous EMA relationship per ticker for crossover detection.
        self._prev_fast_above: dict[str, bool | None] = {}

    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        ind = snapshot.indicators
        ema_fast = ind.get(f"ema_{self._ema_fast_period}", 0.0)
        ema_slow = ind.get(f"ema_{self._ema_slow_period}", 0.0)
        adx_val = ind.get("adx_14", 0.0)
        atr_val = ind.get("atr_14", 0.0)

        if ema_fast == 0 or ema_slow == 0:
            return []

        fast_above = ema_fast > ema_slow
        prev = self._prev_fast_above.get(snapshot.ticker)
        self._prev_fast_above[snapshot.ticker] = fast_above

        if prev is None:
            return []

        # No crossover this tick.
        if fast_above == prev:
            return []

        # ADX filter — only trade when there's some trend.
        if adx_val < self._adx_threshold:
            return []

        # Direction from crossover (market structure is optional confirmation).
        if fast_above:
            side = "buy"
        else:
            side = "sell"

        sl_pct = (atr_val / snapshot.quote.last * self._atr_sl_mult * 100) if snapshot.quote.last > 0 else 2.0
        tp_pct = sl_pct * 1.5

        confidence = min(1.0, adx_val / 50.0)

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
                    "ema_fast": round(ema_fast, 4),
                    "ema_slow": round(ema_slow, 4),
                    "adx": round(adx_val, 2),
                    "atr": round(atr_val, 4),
                },
            )
        ]
