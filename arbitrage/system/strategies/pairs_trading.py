"""
Statistical Arbitrage / Pairs Trading Strategy.

Trades mean-reversion of correlated asset pairs on futures.
When the spread (z-score) between two correlated assets deviates
significantly from the mean, we go long the underperformer and
short the outperformer, expecting reversion.

Examples: BTC/ETH, SOL/AVAX, DOGE/SHIB, BNB/OKB
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from arbitrage.system.models import (
    MarketSnapshot,
    OrderBookSnapshot,
    StrategyId,
    TradeIntent,
)
from arbitrage.system.strategies.base import BaseStrategy

logger = logging.getLogger("trading_system")

# Predefined correlated pairs for stat-arb
# (asset_a, asset_b, expected_correlation)
_STAT_ARB_PAIRS: List[Tuple[str, str, float]] = [
    ("BTCUSDT", "ETHUSDT", 0.90),
    ("SOLUSDT", "AVAXUSDT", 0.80),
    ("DOGEUSDT", "SHIBUSDT", 0.75),
    ("LINKUSDT", "DOTUSDT", 0.70),
    ("ADAUSDT", "XRPUSDT", 0.72),
    ("ARBUSDT", "OPUSDT", 0.82),
    ("APTUSDT", "SUIUSDT", 0.78),
]


@dataclass
class SpreadHistory:
    """Rolling window of spread values for z-score calculation."""
    values: deque = field(default_factory=lambda: deque(maxlen=500))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=500))

    def add(self, value: float, ts: float) -> None:
        self.values.append(value)
        self.timestamps.append(ts)

    @property
    def mean(self) -> float:
        if not self.values:
            return 0.0
        return sum(self.values) / len(self.values)

    @property
    def std(self) -> float:
        if len(self.values) < 2:
            return 1.0
        m = self.mean
        var = sum((v - m) ** 2 for v in self.values) / (len(self.values) - 1)
        return max(math.sqrt(var), 1e-10)

    @property
    def zscore(self) -> float:
        if len(self.values) < 20:
            return 0.0
        return (self.values[-1] - self.mean) / self.std

    @property
    def count(self) -> int:
        return len(self.values)


class PairsTradingStrategy(BaseStrategy):
    """
    Statistical arbitrage using correlated asset pairs.

    Monitors the log-price ratio (spread) between two assets.
    When z-score exceeds threshold, opens hedged position expecting
    mean reversion.

    Works on a SINGLE exchange using perpetual futures.
    """

    def __init__(
        self,
        entry_zscore: float = 2.0,
        exit_zscore: float = 0.5,
        max_zscore: float = 5.0,
        min_history: int = 50,
        lookback_window: int = 500,
        min_profit_bps: float = 5.0,
        cooldown_sec: float = 60.0,
        preferred_exchange: str = "bybit",
        min_cointegration_score: float = 0.6,
    ):
        super().__init__(StrategyId.PAIRS_TRADING)
        self.entry_zscore = entry_zscore
        self.exit_zscore = exit_zscore
        self.max_zscore = max_zscore
        self.min_history = min_history
        self.lookback_window = lookback_window
        self.min_profit_bps = min_profit_bps
        self.cooldown_sec = cooldown_sec
        self.preferred_exchange = preferred_exchange
        # FIX #12: Cointegration validation — correlation != mean reversion
        # Augmented Dickey-Fuller proxy: we use Hurst exponent approximation
        # to verify the spread is actually stationary (mean-reverting).
        self.min_cointegration_score = min_cointegration_score

        # Spread history per (exchange, pair_key)
        self._spreads: Dict[str, SpreadHistory] = {}
        self._last_signal_ts: Dict[str, float] = {}
        # Cointegration scores per pair_key (computed online)
        self._cointegration_scores: Dict[str, float] = {}

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        """
        Update spread history and check for entry signals.

        Called per-symbol. We update spread for any pair that includes
        this symbol, then check if z-score crosses entry threshold.
        """
        intents: List[TradeIntent] = []
        now = time.time()
        symbol = snapshot.symbol

        # Find pairs that include this symbol
        relevant_pairs = [
            (a, b, corr) for a, b, corr in _STAT_ARB_PAIRS
            if a == symbol or b == symbol
        ]
        if not relevant_pairs:
            return intents

        # Update spread for each exchange
        for exchange in snapshot.orderbooks:
            ob = snapshot.orderbooks.get(exchange)
            if not ob or ob.mid <= 0:
                continue

            for asset_a, asset_b, expected_corr in relevant_pairs:
                pair_key = f"{exchange}_{asset_a}_{asset_b}"

                # We can only update spread when we have the current symbol's price
                # The other symbol's price is stored from previous snapshots
                self._update_spread(pair_key, symbol, ob.mid, now)

                # Check for signal
                cooldown_key = f"pairs_{pair_key}"
                if now - self._last_signal_ts.get(cooldown_key, 0.0) < self.cooldown_sec:
                    continue

                intent = self._check_signal(
                    snapshot, exchange, asset_a, asset_b,
                    pair_key, expected_corr, now,
                )
                if intent:
                    self._last_signal_ts[cooldown_key] = now
                    intents.append(intent)

        return intents

    def _update_spread(
        self, pair_key: str, symbol: str, price: float, ts: float,
    ) -> None:
        """Update the spread history with new price data."""
        if pair_key not in self._spreads:
            self._spreads[pair_key] = SpreadHistory()

        # Store prices separately so we can compute ratio when both update
        price_key_a = f"{pair_key}_price_a"
        price_key_b = f"{pair_key}_price_b"

        # Determine which asset this price belongs to
        parts = pair_key.split("_", 1)
        if len(parts) < 2:
            return
        _, pair_str = parts[0], parts[1]
        asset_a, asset_b = pair_str.split("_", 1)

        if symbol == asset_a:
            self.__dict__[price_key_a] = price
        elif symbol == asset_b:
            self.__dict__[price_key_b] = price
        else:
            return

        # Compute spread if we have both prices
        pa = self.__dict__.get(price_key_a)
        pb = self.__dict__.get(price_key_b)
        if pa and pb and pa > 0 and pb > 0:
            log_ratio = math.log(pa / pb)
            self._spreads[pair_key].add(log_ratio, ts)

    def _check_signal(
        self,
        snapshot: MarketSnapshot,
        exchange: str,
        asset_a: str,
        asset_b: str,
        pair_key: str,
        expected_corr: float,
        now: float,
        require_cointegration: bool = True,
    ) -> Optional[TradeIntent]:
        """Check if z-score crossed entry threshold."""
        hist = self._spreads.get(pair_key)
        if not hist or hist.count < self.min_history:
            return None

        z = hist.zscore
        abs_z = abs(z)

        if abs_z < self.entry_zscore or abs_z > self.max_zscore:
            return None

        # FIX #12: Cointegration check — verify spread is stationary
        # Use Hurst exponent approximation: if spread is mean-reverting,
        # variance ratio test should show sub-diffusive behavior.
        cointegration_score = self._compute_hurst_exponent(hist)
        if require_cointegration:
            if cointegration_score < self.min_cointegration_score:
                logger.debug(
                    "[PAIRS_COINT_FAIL] %s: hurst=%.3f < %.3f — spread not stationary, rejecting",
                    pair_key, cointegration_score, self.min_cointegration_score,
                )
                return None

        # z > 0 means A is relatively expensive vs B -> short A, long B
        # z < 0 means B is relatively expensive vs A -> long A, short B
        if z > 0:
            long_symbol = asset_b
            short_symbol = asset_a
            side = "pairs_short_a"
        else:
            long_symbol = asset_a
            short_symbol = asset_b
            side = "pairs_short_b"

        # Expected profit: z-score reversion to exit_zscore
        # Each 1 std = roughly (std of log returns * 100) bps
        std_pct = hist.std * 100
        expected_reversion_std = abs_z - self.exit_zscore
        expected_profit_bps = expected_reversion_std * std_pct * 100

        if expected_profit_bps < self.min_profit_bps:
            return None

        confidence = min(1.0, abs_z / self.max_zscore)

        intent = TradeIntent(
            strategy_id=StrategyId.PAIRS_TRADING,
            symbol=snapshot.symbol,
            long_exchange=exchange,
            short_exchange=exchange,
            side=side,
            confidence=confidence,
            expected_edge_bps=expected_profit_bps,
            stop_loss_bps=expected_profit_bps * 2,
            metadata={
                "strategy_type": "pairs_trading",
                "asset_a": asset_a,
                "asset_b": asset_b,
                "long_symbol": long_symbol,
                "short_symbol": short_symbol,
                "zscore": z,
                "spread_mean": hist.mean,
                "spread_std": hist.std,
                "history_count": hist.count,
                "expected_corr": expected_corr,
                "expected_profit_bps": expected_profit_bps,
                "exchange": exchange,
                "cointegration_score": cointegration_score,
            },
        )

    @staticmethod
    def _compute_hurst_exponent(hist: "SpreadHistory") -> float:
        """
        Compute a simplified Hurst exponent to detect mean reversion.

        Hurst < 0.5 → mean-reverting (good for pairs trading).
        Hurst = 0.5 → random walk.
        Hurst > 0.5 → trending (bad for pairs trading).

        We use the variance ratio method: if Var(2-day returns) / Var(1-day returns)
        is significantly less than 2, the series is mean-reverting.

        Returns a score from 0..1 where 1 = strongly mean-reverting,
        0.5 = random walk, < 0.5 = trending.
        """
        values = list(hist.values)
        if len(values) < 20:
            return 0.5  # Not enough data

        changes = [values[i] - values[i - 1] for i in range(1, len(values))]

        # Variance of 1-step changes
        if len(changes) < 10:
            return 0.5

        mean_ch = sum(changes) / len(changes)
        var_1 = sum((c - mean_ch) ** 2 for c in changes) / len(changes)
        if var_1 < 1e-12:
            return 0.5

        # Variance of 2-step aggregated changes
        changes_2 = [values[i] - values[i - 2] for i in range(2, len(values))]
        if len(changes_2) < 10:
            return 0.5
        mean_ch2 = sum(changes_2) / len(changes_2)
        var_2 = sum((c - mean_ch2) ** 2 for c in changes_2) / len(changes_2)

        # Variance ratio: for random walk, var_2 ≈ 2 * var_1
        vr = var_2 / (2 * var_1)

        # Transform to score: lower VR → more mean-reverting → higher score
        # Score = 1 - VR, clamped to [0, 1]
        score = max(0.0, min(1.0, 1.0 - vr))

        # Cache score for future lookups
        return score

        logger.info(
            "[PAIRS] %s/%s on %s: z=%.2f, profit_est=%.1f bps, history=%d",
            asset_a, asset_b, exchange, z, expected_profit_bps, hist.count,
        )

        return intent
