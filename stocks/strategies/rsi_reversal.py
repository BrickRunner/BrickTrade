from __future__ import annotations

import logging
from typing import List

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


class RsiReversalStrategy(StockBaseStrategy):
    """RSI overbought/oversold reversal.

    Entry:
        - RSI crosses back UP through ``oversold`` (e.g. 30) → buy.
        - RSI crosses back DOWN through ``overbought`` (e.g. 70) → sell.
    Filter:
        - ADX < ``adx_max`` (avoid trading reversals in strong trends).
    Stop-loss:
        - ATR-based.
    """

    def __init__(
        self,
        oversold: float = 30.0,
        overbought: float = 70.0,
        adx_max: float = 30.0,
        atr_sl_mult: float = 1.5,
        quantity_lots: int = 1,
    ) -> None:
        super().__init__(StockStrategyId.RSI_REVERSAL)
        self._oversold = oversold
        self._overbought = overbought
        self._adx_max = adx_max
        self._atr_sl_mult = atr_sl_mult
        self._quantity_lots = quantity_lots
        # Previous RSI per ticker.
        self._prev_rsi: dict[str, float] = {}

    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        ind = snapshot.indicators
        current_rsi = ind.get("rsi_14", 50.0)
        adx_val = ind.get("adx_14", 0.0)
        atr_val = ind.get("atr_14", 0.0)
        price = snapshot.quote.last
        ticker = snapshot.ticker

        prev_rsi = self._prev_rsi.get(ticker)
        self._prev_rsi[ticker] = current_rsi

        if prev_rsi is None or price <= 0:
            return []

        # ADX filter — avoid strong trends.
        if adx_val > self._adx_max:
            return []

        side = None

        # RSI crosses UP through oversold → buy.
        if prev_rsi < self._oversold <= current_rsi:
            side = "buy"

        # RSI crosses DOWN through overbought → sell.
        if prev_rsi > self._overbought >= current_rsi:
            side = "sell"

        if side is None:
            return []

        sl_pct = (atr_val * self._atr_sl_mult / price * 100) if atr_val > 0 else 2.0
        tp_pct = sl_pct * 1.5

        # Confidence: how far into the reversal zone the previous RSI was.
        if side == "buy":
            depth = max(0, self._oversold - prev_rsi) / self._oversold
        else:
            depth = max(0, prev_rsi - self._overbought) / (100 - self._overbought)
        confidence = min(1.0, 0.5 + depth)

        return [
            StockTradeIntent(
                strategy_id=self._strategy_id,
                ticker=ticker,
                side=side,
                quantity_lots=self._quantity_lots,
                confidence=confidence,
                expected_edge_pct=sl_pct * 0.5,
                stop_loss_pct=round(sl_pct, 2),
                take_profit_pct=round(tp_pct, 2),
                mode=self.default_mode,
                metadata={
                    "prev_rsi": round(prev_rsi, 2),
                    "current_rsi": round(current_rsi, 2),
                    "adx": round(adx_val, 2),
                },
            )
        ]
