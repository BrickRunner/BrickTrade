from __future__ import annotations

import logging
from typing import List

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


class DivergenceStrategy(StockBaseStrategy):
    """Price/indicator divergence — counter-trend strategy.

    Bullish divergence: price makes lower low, RSI makes higher low → buy.
    Bearish divergence: price makes higher high, RSI makes lower high → sell.

    Higher risk, so default mode is semi_auto (user confirmation).
    """

    def __init__(
        self,
        rsi_period: int = 14,
        lookback: int = 30,
        quantity_lots: int = 1,
    ) -> None:
        super().__init__(StockStrategyId.DIVERGENCE)
        self._rsi_period = rsi_period
        self._lookback = lookback
        self._quantity_lots = quantity_lots
        # Track recent swing-point data per ticker.
        self._price_highs: dict[str, List[float]] = {}
        self._price_lows: dict[str, List[float]] = {}
        self._rsi_at_highs: dict[str, List[float]] = {}
        self._rsi_at_lows: dict[str, List[float]] = {}

    @property
    def default_mode(self) -> str:
        return "semi_auto"

    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        if len(snapshot.candles) < self._lookback:
            return []

        ind = snapshot.indicators
        current_rsi = ind.get("rsi_14", 50.0)
        bb_width = ind.get("bb_width", 0.0)
        price = snapshot.quote.last
        ticker = snapshot.ticker

        if price <= 0:
            return []

        # Build swing points from candles.
        candles = snapshot.candles[-self._lookback:]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        # Simple 3-bar pivot detection.
        swing_highs_price: List[float] = []
        swing_lows_price: List[float] = []
        swing_highs_idx: List[int] = []
        swing_lows_idx: List[int] = []

        for i in range(1, len(highs) - 1):
            if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
                swing_highs_price.append(highs[i])
                swing_highs_idx.append(i)
            if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
                swing_lows_price.append(lows[i])
                swing_lows_idx.append(i)

        intents: List[StockTradeIntent] = []

        # Bearish divergence: price HH, RSI LH.
        if len(swing_highs_price) >= 2:
            price_hh = swing_highs_price[-1] > swing_highs_price[-2]
            # Need actual RSI values at swing points — approximate with current RSI.
            # Require RSI to be genuinely weak (below 50) while price makes new high.
            rsi_weak = current_rsi < 50
            # Require meaningful price divergence (>0.3% between swing highs).
            price_diff_pct = abs(swing_highs_price[-1] - swing_highs_price[-2]) / swing_highs_price[-2] * 100
            if price_hh and rsi_weak and price_diff_pct > 0.3:
                intents.append(
                    self._make_intent(ticker, "sell", current_rsi, price, "bearish_div", bb_width, price_diff_pct)
                )

        # Bullish divergence: price LL, RSI HL.
        if len(swing_lows_price) >= 2:
            price_ll = swing_lows_price[-1] < swing_lows_price[-2]
            # Require RSI to be genuinely strong (above 50) while price makes new low.
            rsi_strong = current_rsi > 50
            price_diff_pct = abs(swing_lows_price[-1] - swing_lows_price[-2]) / swing_lows_price[-2] * 100
            if price_ll and rsi_strong and price_diff_pct > 0.3:
                intents.append(
                    self._make_intent(ticker, "buy", current_rsi, price, "bullish_div", bb_width, price_diff_pct)
                )

        return intents

    def _make_intent(
        self, ticker: str, side: str, rsi_val: float, price: float,
        div_type: str, bb_width: float, price_diff_pct: float = 0.0,
    ) -> StockTradeIntent:
        # Confidence based on RSI extremity and price divergence strength.
        # RSI component: how far RSI is from neutral (50).
        rsi_extremity = abs(rsi_val - 50.0) / 50.0  # 0..1
        # Price divergence component: bigger swing diff = stronger signal.
        div_strength = min(1.0, price_diff_pct / 1.5)  # 0..1 (1.5% = max)
        confidence = min(1.0, 0.3 * rsi_extremity + 0.7 * div_strength)

        return StockTradeIntent(
            strategy_id=self._strategy_id,
            ticker=ticker,
            side=side,
            quantity_lots=self._quantity_lots,
            confidence=confidence,
            expected_edge_pct=max(0.2, price_diff_pct * 0.5),
            stop_loss_pct=4.0,
            take_profit_pct=6.0,
            mode=self.default_mode,
            metadata={
                "divergence_type": div_type,
                "rsi": round(rsi_val, 2),
                "bb_width": round(bb_width, 4),
                "price_diff_pct": round(price_diff_pct, 2),
            },
        )
