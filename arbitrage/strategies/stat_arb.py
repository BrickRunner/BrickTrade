"""
Strategy C: Statistical Arbitrage

Mean-reversion trading using spread z-score statistics.

For each exchange pair (OKX↔HTX, OKX↔Bybit, HTX↔Bybit), maintain a rolling
history of the price spread for each symbol. Compute z-score and trade when
the spread deviates significantly from its mean.

Detection:
    mean = rolling_mean(spread_history)
    std = rolling_std(spread_history)
    z_score = (current_spread - mean) / std

Entry:
    |z_score| > z_entry (default 2.5)
    → spread is abnormally high, expect reversion

Exit:
    |z_score| < z_exit (default 0.5)
    → spread has reverted to mean

Direction:
    z > +2.5: spread is too high → short the spread (short high, long low)
    z < -2.5: spread is too low → long the spread (long high, short low)
"""
import math
from collections import deque
from typing import Dict, List, Tuple

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.market_data import MarketDataEngine
from arbitrage.core.state import ActivePosition
from arbitrage.strategies.base import BaseStrategy, Opportunity, StrategyType

logger = get_arbitrage_logger("stat_arb")

# Minimum samples before we trust statistics
MIN_SAMPLES = 30

# Round-trip fees
ROUND_TRIP_FEE_PCT = 0.16  # 4 legs × 0.04%


class SpreadTracker:
    """Rolling statistics for one (exchange_pair, symbol) spread."""

    __slots__ = ("_data", "_sum", "_sum_sq", "_maxlen")

    def __init__(self, maxlen: int = 500):
        self._data: deque = deque(maxlen=maxlen)
        self._sum: float = 0.0
        self._sum_sq: float = 0.0
        self._maxlen = maxlen

    def add(self, spread: float) -> None:
        if len(self._data) == self._maxlen:
            old = self._data[0]
            self._sum -= old
            self._sum_sq -= old * old
        self._data.append(spread)
        self._sum += spread
        self._sum_sq += spread * spread

    @property
    def count(self) -> int:
        return len(self._data)

    @property
    def mean(self) -> float:
        n = len(self._data)
        if n == 0:
            return 0.0
        return self._sum / n

    @property
    def std(self) -> float:
        n = len(self._data)
        if n < 2:
            return 0.0
        variance = (self._sum_sq / n) - (self._sum / n) ** 2
        return math.sqrt(max(0.0, variance))

    def z_score(self, value: float) -> float:
        s = self.std
        if s < 1e-10:
            return 0.0
        return (value - self.mean) / s


class StatArbStrategy(BaseStrategy):

    def __init__(self, config: ArbitrageConfig, market_data: MarketDataEngine):
        self.config = config
        self.market_data = market_data
        self._z_entry = config.stat_arb_z_entry
        self._z_exit = config.stat_arb_z_exit
        self._window = config.stat_arb_window

        # Spread trackers: {(ex1, ex2, symbol): SpreadTracker}
        self._trackers: Dict[tuple, SpreadTracker] = {}

    @property
    def name(self) -> str:
        return "stat_arb"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.STAT_ARB

    def get_threshold(self, _symbol: str) -> float:
        return self._z_entry

    def _get_tracker(self, ex1: str, ex2: str, symbol: str) -> SpreadTracker:
        key = (ex1, ex2, symbol)
        if key not in self._trackers:
            self._trackers[key] = SpreadTracker(maxlen=self._window)
        return self._trackers[key]

    def update_spreads(self, market_data: MarketDataEngine) -> None:
        """
        Called every cycle to feed new spread data into trackers.
        Must be called before detect_opportunities.
        """
        exchanges = market_data.get_exchange_names()
        for i, ex1 in enumerate(exchanges):
            for ex2 in exchanges[i + 1:]:
                for sym in market_data.common_pairs:
                    p1 = market_data.get_futures_price(ex1, sym)
                    p2 = market_data.get_futures_price(ex2, sym)
                    if not p1 or not p2:
                        continue
                    mid1 = (p1.bid + p1.ask) / 2
                    mid2 = (p2.bid + p2.ask) / 2
                    if mid1 <= 0 or mid2 <= 0:
                        continue
                    spread = (mid1 - mid2) / mid2 * 100
                    tracker = self._get_tracker(ex1, ex2, sym)
                    tracker.add(spread)

    async def detect_opportunities(self, market_data: MarketDataEngine) -> List[Opportunity]:
        """
        Find symbols where the current spread z-score exceeds entry threshold.
        """
        self.update_spreads(market_data)
        opportunities: List[Opportunity] = []
        exchanges = market_data.get_exchange_names()

        for i, ex1 in enumerate(exchanges):
            for ex2 in exchanges[i + 1:]:
                for sym in market_data.common_pairs:
                    tracker = self._trackers.get((ex1, ex2, sym))
                    if not tracker or tracker.count < MIN_SAMPLES:
                        continue

                    p1 = market_data.get_futures_price(ex1, sym)
                    p2 = market_data.get_futures_price(ex2, sym)
                    if not p1 or not p2:
                        continue

                    mid1 = (p1.bid + p1.ask) / 2
                    mid2 = (p2.bid + p2.ask) / 2
                    if mid1 <= 0 or mid2 <= 0:
                        continue

                    current_spread = (mid1 - mid2) / mid2 * 100
                    z = tracker.z_score(current_spread)

                    if abs(z) < self._z_entry:
                        continue

                    # Expected profit = distance from mean in %, minus fees
                    expected = abs(current_spread - tracker.mean) - ROUND_TRIP_FEE_PCT
                    if expected <= 0:
                        continue

                    if z > 0:
                        # Spread is high: ex1 expensive vs ex2
                        # Short ex1, long ex2 → expect convergence
                        long_ex, short_ex = ex2, ex1
                        long_price = p2.ask
                        short_price = p1.bid
                    else:
                        # Spread is low: ex1 cheap vs ex2
                        # Long ex1, short ex2
                        long_ex, short_ex = ex1, ex2
                        long_price = p1.ask
                        short_price = p2.bid

                    opportunities.append(Opportunity(
                        strategy=StrategyType.STAT_ARB,
                        symbol=sym,
                        long_exchange=long_ex,
                        short_exchange=short_ex,
                        expected_profit_pct=expected,
                        long_price=long_price,
                        short_price=short_price,
                        confidence=min(1.0, abs(z) / (self._z_entry + 1)),
                        metadata={
                            "z_score": z,
                            "mean": tracker.mean,
                            "std": tracker.std,
                            "samples": tracker.count,
                            "current_spread": current_spread,
                        },
                    ))

        opportunities.sort(key=lambda o: abs(o.metadata.get("z_score", 0)), reverse=True)
        return opportunities

    async def should_exit(
        self, position: ActivePosition, market_data: MarketDataEngine
    ) -> Tuple[bool, str]:
        """
        Exit when z-score reverts to within exit threshold of mean.
        """
        sym = position.symbol
        long_ex = position.long_exchange
        short_ex = position.short_exchange

        # Determine tracker key order
        exchanges = market_data.get_exchange_names()
        if long_ex in exchanges and short_ex in exchanges:
            if exchanges.index(long_ex) < exchanges.index(short_ex):
                ex1, ex2 = long_ex, short_ex
            else:
                ex1, ex2 = short_ex, long_ex
        else:
            ex1, ex2 = long_ex, short_ex

        tracker = self._trackers.get((ex1, ex2, sym))
        if not tracker:
            # Try reverse
            tracker = self._trackers.get((ex2, ex1, sym))

        if not tracker or tracker.count < MIN_SAMPLES:
            # Can't compute z-score — exit on timeout only
            if position.duration() > 3600:
                return True, "no_data_timeout"
            return False, ""

        p1 = market_data.get_futures_price(ex1, sym)
        p2 = market_data.get_futures_price(ex2, sym)
        if not p1 or not p2:
            return False, ""

        mid1 = (p1.bid + p1.ask) / 2
        mid2 = (p2.bid + p2.ask) / 2
        if mid1 <= 0 or mid2 <= 0:
            return False, ""

        current_spread = (mid1 - mid2) / mid2 * 100
        z = tracker.z_score(current_spread)

        if abs(z) < self._z_exit:
            return True, "z_score_reverted"

        # Stop loss: z-score went even further (spread diverged more)
        if abs(z) > self._z_entry + 2.0:
            return True, "stop_loss_z_extreme"

        # Timeout: 1h
        if position.duration() > 3600:
            return True, "timeout_1h"

        return False, ""

    def get_all_spreads(self, market_data: MarketDataEngine) -> list:
        """Return z-scores for display."""
        items = []
        for (ex1, ex2, sym), tracker in self._trackers.items():
            if tracker.count < MIN_SAMPLES:
                continue
            p1 = market_data.get_futures_price(ex1, sym)
            p2 = market_data.get_futures_price(ex2, sym)
            if not p1 or not p2:
                continue
            mid1 = (p1.bid + p1.ask) / 2
            mid2 = (p2.bid + p2.ask) / 2
            if mid2 <= 0:
                continue
            current = (mid1 - mid2) / mid2 * 100
            z = tracker.z_score(current)
            items.append({
                "symbol": sym,
                "z_score": z,
                "mean": tracker.mean,
                "std": tracker.std,
                "current_spread": current,
                "ex1": ex1,
                "ex2": ex2,
                "samples": tracker.count,
            })
        items.sort(key=lambda x: abs(x["z_score"]), reverse=True)
        return items
