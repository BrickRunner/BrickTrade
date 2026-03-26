from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OverheatSignal:
    symbol: str
    score: int
    is_overheated: bool
    signals: Dict[str, bool]
    meta: Dict[str, float]
    timestamp: float = field(default_factory=time.time)


DEFAULT_CONFIG: Dict[str, Any] = {
    "rsi_period": 14,
    "rsi_threshold": 80,
    "price_change_threshold": 5.0,
    "price_change_lookback": 10,
    "volume_multiplier": 2.0,
    "volume_lookback": 20,
    "ema_fast": 50,
    "ema_slow": 200,
    "ema_deviation_threshold": 3.0,
    "divergence_lookback": 5,
    "score_threshold": 4,
    "trend_filter_min_score": 6,
}


class OverheatDetector:

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config: Dict[str, Any] = {**DEFAULT_CONFIG, **(config or {})}

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> List[float]:
        if len(closes) < period + 1:
            return [50.0] * len(closes)

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas]
        losses = [abs(min(d, 0.0)) for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        rsi_series: List[float] = [50.0] * period  # padding for first `period` values

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

            if avg_loss == 0:
                rsi_series.append(100.0 if avg_gain > 0 else 50.0)
            else:
                rs = avg_gain / avg_loss
                rsi_series.append(100.0 - (100.0 / (1.0 + rs)))

        return rsi_series

    @staticmethod
    def calculate_ema(values: List[float], period: int) -> List[float]:
        if not values:
            return []
        multiplier = 2.0 / (period + 1)
        ema = [values[0]]
        for i in range(1, len(values)):
            ema.append(values[i] * multiplier + ema[-1] * (1.0 - multiplier))
        return ema

    # ------------------------------------------------------------------
    # Main analysis
    # ------------------------------------------------------------------

    def analyze(
        self,
        symbol: str,
        ohlcv: List[Candle],
        funding_rate: Optional[float] = None,
        oi_history: Optional[List[float]] = None,
    ) -> OverheatSignal:
        cfg = self.config
        closes = [c.close for c in ohlcv]
        volumes = [c.volume for c in ohlcv]

        neutral = OverheatSignal(
            symbol=symbol,
            score=0,
            is_overheated=False,
            signals={k: False for k in (
                "rsi", "price_spike", "volume_spike",
                "funding", "oi_growth", "ema_deviation", "divergence",
            )},
            meta={"rsi_value": 50.0, "price_change": 0.0,
                  "volume_ratio": 0.0, "ema_distance": 0.0},
        )

        min_candles = cfg["rsi_period"] + 1
        if len(closes) < min_candles:
            return neutral

        # --- Indicators ---
        rsi_series = self.calculate_rsi(closes, cfg["rsi_period"])

        has_ema_fast = len(closes) >= cfg["ema_fast"]
        has_ema_slow = len(closes) >= cfg["ema_slow"]

        ema_fast_series = self.calculate_ema(closes, cfg["ema_fast"]) if has_ema_fast else []
        ema_slow_series = self.calculate_ema(closes, cfg["ema_slow"]) if has_ema_slow else []

        # --- 1. RSI ---
        rsi_value = rsi_series[-1]
        signal_rsi = rsi_value > cfg["rsi_threshold"]

        # --- 2. Price Spike ---
        lookback = min(cfg["price_change_lookback"], len(closes) - 1)
        price_prev = closes[-(lookback + 1)] if lookback > 0 else closes[0]
        price_change = ((closes[-1] - price_prev) / price_prev * 100.0) if price_prev != 0 else 0.0
        signal_price = price_change > cfg["price_change_threshold"]

        # --- 3. Volume Spike ---
        vol_lb = min(cfg["volume_lookback"], len(volumes))
        volume_avg = sum(volumes[-vol_lb:]) / vol_lb if vol_lb > 0 else 0.0
        volume_ratio = volumes[-1] / volume_avg if volume_avg > 0 else 0.0
        signal_volume = volume_ratio > cfg["volume_multiplier"]

        # --- 4. Funding Rate ---
        signal_funding = funding_rate is not None and funding_rate > 0

        # --- 5. Open Interest Growth ---
        signal_oi = False
        if oi_history and len(oi_history) >= 5:
            signal_oi = oi_history[-1] > oi_history[-5]

        # --- 6. EMA Deviation ---
        ema_distance = 0.0
        signal_ema = False
        if ema_fast_series and ema_fast_series[-1] != 0:
            ema_distance = (closes[-1] - ema_fast_series[-1]) / ema_fast_series[-1] * 100.0
            signal_ema = ema_distance > cfg["ema_deviation_threshold"]

        # --- 7. Bearish Divergence ---
        signal_divergence = False
        div_lb = min(cfg["divergence_lookback"], len(closes), len(rsi_series))
        if div_lb >= 2:
            price_high = max(closes[-div_lb:])
            rsi_high = max(rsi_series[-div_lb:])
            signal_divergence = closes[-1] >= price_high and rsi_series[-1] < rsi_high

        # --- Scoring ---
        signals_map = {
            "rsi": signal_rsi,
            "price_spike": signal_price,
            "volume_spike": signal_volume,
            "funding": signal_funding,
            "oi_growth": signal_oi,
            "ema_deviation": signal_ema,
            "divergence": signal_divergence,
        }
        score = sum(signals_map.values())
        is_overheated = score >= cfg["score_threshold"]

        # --- Filters ---
        # Trend filter
        if ema_slow_series and closes[-1] > ema_slow_series[-1]:
            if score < cfg["trend_filter_min_score"]:
                is_overheated = False

        # Price spike filter
        if not signal_price:
            is_overheated = False

        signal = OverheatSignal(
            symbol=symbol,
            score=score,
            is_overheated=is_overheated,
            signals=signals_map,
            meta={
                "rsi_value": round(rsi_value, 2),
                "price_change": round(price_change, 4),
                "volume_ratio": round(volume_ratio, 4),
                "ema_distance": round(ema_distance, 4),
            },
        )

        return self.add_custom_filter(signal)

    # ------------------------------------------------------------------
    # Extension hooks
    # ------------------------------------------------------------------

    def add_custom_filter(self, signal: OverheatSignal) -> OverheatSignal:
        return signal

    def add_orderbook_analysis(self, signal: OverheatSignal, data: Any) -> OverheatSignal:
        return signal

    def add_liquidation_data(self, signal: OverheatSignal, data: Any) -> OverheatSignal:
        return signal
