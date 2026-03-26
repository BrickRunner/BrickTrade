from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

from stocks.strategies.base import StockBaseStrategy
from stocks.system.models import StockSnapshot, StockStrategyId, StockTradeIntent

logger = logging.getLogger(__name__)


class MeanReversionStrategy(StockBaseStrategy):
    """Pair-trading mean-reversion on correlated stocks.

    Tracks the price ratio of two tickers. When the z-score of the ratio
    exceeds ``zscore_entry``, opens a long/short pair.  Exits when the
    z-score reverts to ``zscore_exit``.
    """

    def __init__(
        self,
        pairs: List[str],
        zscore_entry: float = 2.0,
        zscore_exit: float = 0.5,
        window: int = 50,
        quantity_lots: int = 1,
    ) -> None:
        super().__init__(StockStrategyId.MEAN_REVERSION)
        # pairs: ["SBER:SBERP", "GAZP:LKOH"]
        self._pairs = [tuple(p.split(":")) for p in pairs]
        self._zscore_entry = zscore_entry
        self._zscore_exit = zscore_exit
        self._window = window
        self._quantity_lots = quantity_lots
        # Internal spread history per pair.
        self._spread_history: Dict[tuple, List[float]] = {p: [] for p in self._pairs}
        # Track whether we have an open signal per pair.
        self._open_signal: Dict[tuple, Optional[str]] = {p: None for p in self._pairs}
        # Cache latest prices by ticker for cross-snapshot use.
        self._latest_prices: Dict[str, float] = {}

    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]:
        ticker = snapshot.ticker
        if snapshot.quote.last > 0:
            self._latest_prices[ticker] = snapshot.quote.last

        intents: List[StockTradeIntent] = []

        for pair in self._pairs:
            a, b = pair
            price_a = self._latest_prices.get(a)
            price_b = self._latest_prices.get(b)
            if price_a is None or price_b is None or price_b == 0:
                continue

            ratio = price_a / price_b
            history = self._spread_history[pair]
            history.append(ratio)
            if len(history) > self._window * 3:
                self._spread_history[pair] = history[-self._window * 3:]
                history = self._spread_history[pair]

            if len(history) < self._window:
                continue

            window = history[-self._window:]
            mean = sum(window) / len(window)
            var = sum((x - mean) ** 2 for x in window) / len(window)
            std = math.sqrt(var) if var > 0 else 1e-9
            zscore = (ratio - mean) / std

            signal = self._open_signal[pair]

            # Entry: spread diverged.
            if signal is None:
                if zscore >= self._zscore_entry and ticker == a:
                    # Ratio too high: A overpriced → sell A, buy B.
                    intents.append(self._make_intent(a, "sell", zscore, ratio))
                    self._open_signal[pair] = "sell_a"
                elif zscore <= -self._zscore_entry and ticker == a:
                    # Ratio too low: A underpriced → buy A, sell B.
                    intents.append(self._make_intent(a, "buy", abs(zscore), ratio))
                    self._open_signal[pair] = "buy_a"

            # Exit: spread reverted.
            elif signal == "sell_a" and abs(zscore) <= self._zscore_exit:
                intents.append(self._make_intent(a, "buy", abs(zscore), ratio))
                self._open_signal[pair] = None
            elif signal == "buy_a" and abs(zscore) <= self._zscore_exit:
                intents.append(self._make_intent(a, "sell", abs(zscore), ratio))
                self._open_signal[pair] = None

        return intents

    def _make_intent(
        self, ticker: str, side: str, zscore: float, ratio: float
    ) -> StockTradeIntent:
        return StockTradeIntent(
            strategy_id=self._strategy_id,
            ticker=ticker,
            side=side,
            quantity_lots=self._quantity_lots,
            confidence=min(1.0, abs(zscore) / (self._zscore_entry * 2)),
            expected_edge_pct=abs(zscore) * 0.5,
            stop_loss_pct=4.0,
            take_profit_pct=3.0,
            mode=self.default_mode,
            metadata={"zscore": round(zscore, 3), "ratio": round(ratio, 5)},
        )
