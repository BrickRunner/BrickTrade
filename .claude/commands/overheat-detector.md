# Overheat Detector Implementation

You are implementing the **Overheat Detector** module — a signal generator that identifies overheated (overbought) instruments suitable for SHORT positions.

## What to Build

Create `arbitrage/system/strategies/overheat_detector.py` with a complete, production-ready `OverheatDetector` class.

---

## Architecture Requirements

1. **File location**: `arbitrage/system/strategies/overheat_detector.py`
2. **Must integrate** with the existing strategy framework in `arbitrage/system/strategies/base.py` and `arbitrage/system/models.py`
3. **No external TA libraries** — implement RSI, EMA from scratch using only stdlib + numpy (already in project)
4. **Type hints** on all public methods
5. **Async-compatible** — the `analyze()` method should be a regular sync method (indicators are CPU-bound), but provide an async wrapper if needed

---

## Data Structures

### Input: Candle
```python
@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
```

### Input: Config (with defaults)
```python
DEFAULT_CONFIG = {
    "rsi_period": 14,
    "rsi_threshold": 80,
    "price_change_threshold": 5.0,   # %
    "price_change_lookback": 10,     # candles
    "volume_multiplier": 2.0,
    "volume_lookback": 20,           # candles for avg
    "ema_fast": 50,
    "ema_slow": 200,
    "ema_deviation_threshold": 3.0,  # %
    "divergence_lookback": 5,        # candles
    "score_threshold": 4,
    "trend_filter_min_score": 6,     # if in uptrend, need this score minimum
}
```

### Output: OverheatSignal
```python
@dataclass
class OverheatSignal:
    symbol: str
    score: int
    is_overheated: bool
    signals: Dict[str, bool]   # rsi, price_spike, volume_spike, funding, oi_growth, ema_deviation, divergence
    meta: Dict[str, float]     # rsi_value, price_change, volume_ratio, ema_distance
    timestamp: float
```

---

## Indicator Implementations

Implement these as **static methods** or **module-level functions**:

### RSI (Wilder's smoothing)
```
1. Calculate price changes: delta = close[i] - close[i-1]
2. Separate gains and losses
3. First avg_gain = mean(gains[:period]), first avg_loss = mean(losses[:period])
4. Subsequent: avg_gain = (prev_avg_gain * (period-1) + gain) / period
5. RS = avg_gain / avg_loss
6. RSI = 100 - (100 / (1 + RS))
```
Return the full RSI series (list of floats).

### EMA
```
multiplier = 2 / (period + 1)
ema[0] = close[0]  # or SMA of first `period` values
ema[i] = close[i] * multiplier + ema[i-1] * (1 - multiplier)
```
Return the full EMA series.

---

## Signal Logic (all 7 signals)

### 1. RSI Overbought
```python
signal_rsi = rsi[-1] > config["rsi_threshold"]
```

### 2. Price Spike
```python
lookback = config["price_change_lookback"]
price_change = (close[-1] - close[-lookback]) / close[-lookback] * 100
signal_price = price_change > config["price_change_threshold"]
```

### 3. Volume Spike
```python
vol_lookback = config["volume_lookback"]
volume_avg = mean(volumes[-vol_lookback:])
volume_ratio = volumes[-1] / volume_avg if volume_avg > 0 else 0
signal_volume = volume_ratio > config["volume_multiplier"]
```

### 4. Funding Rate (positive = longs pay shorts = overheated)
```python
signal_funding = funding_rate is not None and funding_rate > 0
```

### 5. Open Interest Growth
```python
signal_oi = False
if oi_history and len(oi_history) >= 5:
    signal_oi = oi_history[-1] > oi_history[-5]
```

### 6. EMA Deviation
```python
ema_f = ema(closes, config["ema_fast"])
distance = (close[-1] - ema_f[-1]) / ema_f[-1] * 100
signal_ema = distance > config["ema_deviation_threshold"]
```

### 7. Bearish Divergence (simplified)
```python
lb = config["divergence_lookback"]
price_high = max(closes[-lb:])
rsi_high = max(rsi_series[-lb:])
signal_divergence = closes[-1] >= price_high and rsi_series[-1] < rsi_high
```

---

## Scoring & Filters

```python
score = sum([signal_rsi, signal_price, signal_volume, signal_funding, signal_oi, signal_ema, signal_divergence])
is_overheated = score >= config["score_threshold"]
```

### Mandatory filters (applied AFTER scoring):

**Trend filter**: If price is above slow EMA (uptrend), require higher score:
```python
ema_s = ema(closes, config["ema_slow"])
if closes[-1] > ema_s[-1] and score < config["trend_filter_min_score"]:
    is_overheated = False
```

**Price spike filter**: No price spike = no signal:
```python
if not signal_price:
    is_overheated = False
```

---

## Class Structure

```python
class OverheatDetector:
    def __init__(self, config: Optional[Dict] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}

    def analyze(self, symbol: str, ohlcv: List[Candle],
                funding_rate: Optional[float] = None,
                oi_history: Optional[List[float]] = None) -> OverheatSignal:
        """Main analysis entry point."""
        # Validate minimum data
        # Calculate all indicators
        # Generate all 7 signals
        # Calculate score
        # Apply filters
        # Return OverheatSignal

    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> List[float]:
        ...

    @staticmethod
    def calculate_ema(values: List[float], period: int) -> List[float]:
        ...

    # Extension hooks
    def add_custom_filter(self, signal: OverheatSignal) -> OverheatSignal:
        """Override in subclass to add custom filters."""
        return signal

    def add_orderbook_analysis(self, signal: OverheatSignal, data: Any) -> OverheatSignal:
        """Override in subclass to incorporate orderbook data."""
        return signal

    def add_liquidation_data(self, signal: OverheatSignal, data: Any) -> OverheatSignal:
        """Override in subclass to incorporate liquidation data."""
        return signal
```

---

## Validation Requirements

After writing the module:

1. Verify it imports correctly:
   ```bash
   python -c "from arbitrage.system.strategies.overheat_detector import OverheatDetector; print('OK')"
   ```

2. Write a quick smoke test inline or as a separate check — create synthetic candles, run `analyze()`, verify the output structure has all expected fields.

3. Ensure no circular imports with existing code.

---

## Edge Cases to Handle

- Less than `ema_slow` candles: skip EMA-based signals, don't crash
- Less than `rsi_period + 1` candles: return neutral signal with `is_overheated=False`
- Zero volume average: avoid division by zero
- Missing funding_rate or oi_history: treat signals as False, don't crash
- All prices identical: RSI should be 50 (neutral), not crash on division by zero

---

## DO NOT

- Do NOT install new packages
- Do NOT modify existing strategy files
- Do NOT add Telegram handlers (this is a pure analysis module)
- Do NOT add logging imports unless using the existing logger from `arbitrage/utils/logger.py`
