---
name: fix-market-intelligence
description: Fix all critical issues in the Market Intelligence System identified during professional audit.
---

# Fix Critical Issues in Market Intelligence System

You are fixing critical bugs, missing features, and architectural flaws in the Market Intelligence System located in `market_intelligence/`.

Read ALL files in `market_intelligence/` before making any changes. Understand the full pipeline: collector -> feature_engine -> regime -> scorer -> portfolio -> engine.

---

## 1. Fix Inverted Funding Divergence Logic

**File:** `market_intelligence/regime.py`, method `_classify`, lines ~99-102

**Bug:** The funding divergence interaction is backwards. Currently:
- `ema_cross_z > 1.0 and funding_z < -0.5` boosts OVERHEATED
- `ema_cross_z < -1.0 and funding_z > 0.5` boosts PANIC

**This is wrong.** Overheated = price rising AND funding highly positive (crowded long). Panic = price falling AND funding highly negative (crowded short).

**Fix:**
```python
# Divergence: trend + extreme funding in same direction = overheated/panic
if ema_cross_z > 1.0 and funding_z > 0.5:
    logits[MarketRegime.OVERHEATED] += 0.3 * s
if ema_cross_z < -1.0 and funding_z < -0.5:
    logits[MarketRegime.PANIC] += 0.3 * s
```

Also add the actual divergence case (funding against price = potential reversal signal):
```python
# Counter-trend funding divergence: warns of potential reversal
if ema_cross_z > 1.0 and funding_z < -0.8:
    logits[MarketRegime.RANGE] += 0.2 * s  # trend may be weakening
if ema_cross_z < -1.0 and funding_z > 0.8:
    logits[MarketRegime.RANGE] += 0.2 * s  # downtrend may be weakening
```

---

## 2. Add Fast-Path Bypass for Extreme Events

**File:** `market_intelligence/regime.py`, method `_apply_stability`

**Problem:** The stability layer delays regime changes by `min_duration_cycles` (default 2 cycles = 10+ minutes at 5min intervals). During a flash crash or extreme pump, the system keeps reporting the old regime.

**Fix:** Add a fast-path that bypasses stability checks when indicators show extreme conditions. Add a method `_is_extreme` and call it before applying stability:

```python
def _is_extreme(self, feature: FeatureVector | None) -> bool:
    """Detect extreme market conditions that should bypass stability delay."""
    if feature is None:
        return False
    v = feature.values
    rsi = float(v.get("rsi") or 50.0)
    vol_spike = float(v.get("volume_spike") or 1.0)
    vol_z = float(v.get("rolling_volatility") or 0.0)

    # Extreme RSI
    if rsi <= 15.0 or rsi >= 85.0:
        return True
    # Massive volume spike
    if vol_spike >= 4.0:
        return True
    # Extreme volatility expansion
    if abs(vol_z) >= 3.0:
        return True
    return False
```

Modify `_classify` to pass the feature to `_apply_stability`, and modify `_apply_stability` to accept an optional `feature` parameter. When `_is_extreme(feature)` returns True, skip the `min_duration_cycles` check — accept the regime change immediately if confidence >= threshold.

Also modify `classify_local` similarly.

---

## 3. Per-Regime Stability Parameters

**File:** `market_intelligence/regime.py`

**Problem:** All regime transitions use the same `min_duration_cycles`. PANIC should activate faster than RANGE.

**Fix:** Add a dict mapping regime to minimum cycles:

```python
REGIME_MIN_CYCLES: Dict[MarketRegime, int] = {
    MarketRegime.PANIC: 1,
    MarketRegime.HIGH_VOLATILITY: 1,
    MarketRegime.OVERHEATED: 1,
    MarketRegime.TREND_UP: 2,
    MarketRegime.TREND_DOWN: 2,
    MarketRegime.RANGE: 3,
}
```

In `_apply_stability`, use `REGIME_MIN_CYCLES.get(candidate_regime, self.min_duration_cycles)` instead of `self.min_duration_cycles` as the required cycles for transition. Keep `self.min_duration_cycles` as a fallback default.

---

## 4. Implement Market Structure (HH/HL, LH/LL)

**File:** `market_intelligence/indicators.py`

**Add** a new function to detect market structure:

```python
def market_structure(highs: List[float], lows: List[float], lookback: int = 20) -> Optional[str]:
    """Detect market structure from swing points.

    Returns: 'bullish' (HH+HL), 'bearish' (LH+LL), 'transition', or None if insufficient data.
    """
```

The function should:
1. Find swing highs and swing lows using a simple 3-bar pivot detection over the last `lookback` bars
2. Compare the last 2 swing highs and last 2 swing lows
3. Return 'bullish' if latest swing high > previous swing high AND latest swing low > previous swing low (HH + HL)
4. Return 'bearish' if latest swing high < previous swing high AND latest swing low < previous swing low (LH + LL)
5. Return 'transition' if mixed (e.g. HH + LL or LH + HL)
6. Return None if insufficient swing points found

**File:** `market_intelligence/feature_engine.py`

Import and compute `market_structure` in `compute()`. Add results to `values` dict:
- `"market_structure_code"`: 1.0 for bullish, -1.0 for bearish, 0.0 for transition, None if unavailable
- Use OHLCV highs/lows when available, fall back to tick-proxy

**File:** `market_intelligence/regime.py`

Use market structure in `_classify` to refine trend logits:
- If market_structure is 'bullish', boost TREND_UP by +0.15 and suppress TREND_DOWN by -0.1
- If market_structure is 'bearish', boost TREND_DOWN by +0.15 and suppress TREND_UP by -0.1
- If market_structure is 'transition', boost RANGE by +0.1

Access it from `feature.values.get("market_structure_code")`.

---

## 5. Remove Dead Orderbook Imbalance Feature or Implement It

**File:** `market_intelligence/collector.py`

**Problem:** `orderbook_imbalance` is always None (lines 182-184) because real orderbook depth is not fetched. But `scorer.py` has logic checking `ob_imb_raw is not None`.

**Fix (Option A — Remove):** If orderbook depth data is NOT available from `MarketDataEngine`:
- Remove the orderbook imbalance confirmation/contradiction logic from `scorer.py` (lines ~156-161). Keep the `ob_imbalance` field in `PairSnapshot` for future use but remove the scoring logic that never fires.
- Add a comment in `collector.py` explaining that orderbook depth is planned but not yet available.

**Fix (Option B — Implement):** If `MarketDataEngine` has a method to fetch orderbook depth (check `arbitrage/core/market_data.py`):
- Compute real imbalance from bid/ask depth volumes
- `imbalance = (total_bid_volume - total_ask_volume) / (total_bid_volume + total_ask_volume)`

Check `arbitrage/core/market_data.py` to determine which option is feasible. Prefer Option B if the data is available.

---

## 6. Fix CVD Proxy Reliability

**File:** `market_intelligence/indicators.py`, function `cumulative_volume_delta`

**Problem:** Current CVD uses close-to-close comparison as buy/sell proxy. This is unreliable — a bar can close higher than previous but have strong selling pressure intrabar.

**Fix:** Improve the proxy by using OHLCV data when available:

```python
def cumulative_volume_delta(
    volumes: List[float], closes: List[float], window: int = 20,
    opens: List[float] | None = None, highs: List[float] | None = None, lows: List[float] | None = None,
) -> float:
    """Normalized CVD over last *window* bars.

    When OHLCV data is available, uses the candle body ratio method:
    buy_pct = (close - low) / (high - low) for better accuracy.
    Falls back to close-to-close comparison otherwise.
    """
```

When opens/highs/lows are provided:
- For each bar: `range = high - low`; if range > 0: `buy_pct = (close - low) / range`, `sell_pct = 1 - buy_pct`
- `buy_vol = volume * buy_pct`, `sell_vol = volume * sell_pct`
- This is the standard approximation used in TradingView and similar platforms

Update `feature_engine.py` to pass OHLCV data to the improved CVD function when candle data is available.

---

## 7. Add Max Allocation Per Pair in Portfolio

**File:** `market_intelligence/portfolio.py`

**Problem:** No cap on single-pair allocation. A top-ranked pair can get 50%+ of capital.

**Fix:** After computing and normalizing allocations (the block where `allocation[s] = 100.0 * allocation[s] / total`), add a cap:

```python
MAX_SINGLE_PAIR_PCT = 25.0

# Cap individual allocation
capped = False
for s in list(allocation):
    if allocation[s] > MAX_SINGLE_PAIR_PCT:
        allocation[s] = MAX_SINGLE_PAIR_PCT
        capped = True

# Redistribute excess proportionally to remaining pairs
if capped:
    total_after_cap = sum(allocation.values())
    if total_after_cap > 0 and abs(total_after_cap - 100.0) > 0.01:
        uncapped = {s: v for s, v in allocation.items() if v < MAX_SINGLE_PAIR_PCT}
        uncapped_total = sum(uncapped.values())
        if uncapped_total > 0:
            excess = 100.0 - total_after_cap
            for s in uncapped:
                allocation[s] += excess * (uncapped[s] / uncapped_total)
```

---

## 8. Add Circuit Breaker for Failed Exchanges

**File:** `market_intelligence/collector.py`

**Problem:** When an exchange API consistently fails, every cycle wastes time on timeouts. No exponential backoff.

**Fix:** Add a simple circuit breaker:

```python
@dataclass
class ExchangeCircuitBreaker:
    exchange: str
    failure_count: int = 0
    last_failure: float = 0.0
    disabled_until: float = 0.0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure = time.time()
        # Exponential backoff: 30s, 60s, 120s, 240s, max 300s
        backoff = min(300.0, 30.0 * (2 ** min(self.failure_count - 1, 4)))
        self.disabled_until = self.last_failure + backoff

    def record_success(self) -> None:
        self.failure_count = 0
        self.disabled_until = 0.0

    def is_available(self) -> bool:
        if self.failure_count == 0:
            return True
        return time.time() >= self.disabled_until
```

Add `_circuit_breakers: Dict[str, ExchangeCircuitBreaker]` to `MarketDataCollector.__init__`.

In `_rate_limited_update` and in the exchange iteration loops (`collect`, `_update_open_interest`, `_update_24h_volume`, `update_slow_data`), skip exchanges where `not circuit_breaker.is_available()`. On success, call `record_success()`. On exception, call `record_failure()`.

Log a warning when an exchange is temporarily disabled and when it's re-enabled.

---

## 9. Fix Redundant Volatility Indicators in Regime Model

**File:** `market_intelligence/regime.py`, method `_classify`

**Problem:** `rolling_volatility` z-score and `bb_width` z-score are highly correlated (both measure price dispersion). Using both in HIGH_VOLATILITY logit double-counts the same signal.

**Fix:** Reduce the weight of the redundant indicator and add ATR percentile as a complementary signal:

```python
MarketRegime.HIGH_VOLATILITY: self.vol_coef * max(0.0, vol_z) * 0.6 + self.bb_coef * max(0.0, bb_z) * 0.4 + atr_pctile_bonus + 0.2,
```

Where `atr_pctile_bonus` uses the ATR percentile rank from features:
```python
atr_pctile = float(v.get("atr_percentile") or 0.5)
atr_pctile_bonus = 0.3 * max(0.0, (atr_pctile - 0.7) / 0.3)  # boost when ATR is in top 30%
```

This makes the three volatility inputs complementary rather than redundant.

---

## Testing Requirements

After ALL changes are made:

1. Run existing tests: `python -m pytest tests/ -v` — ensure nothing breaks
2. Verify the regime model changes by checking that:
   - `_classify` with high ema_cross + high funding → OVERHEATED (not before)
   - `_classify` with extreme RSI (<15) → fast-path to PANIC without stability delay
   - Market structure 'bullish' boosts TREND_UP
3. Verify scorer changes by checking that orderbook imbalance logic is either properly fed with data or removed
4. Verify portfolio max allocation cap works (no single pair > 25%)

Do NOT change test expectations without understanding why. If a test fails, read the test to understand what it's verifying, then decide if the test or the code is wrong.

---

## Important Constraints

- Do NOT change the public API of `MarketIntelligenceEngine.run_once()` or `MarketIntelligenceReport`
- Do NOT change `.env` variable names or add required new env vars (new params should have sensible defaults)
- Do NOT modify files outside `market_intelligence/` unless absolutely necessary
- Keep all changes backward-compatible with existing config
- Preserve all existing logging
- Do not add external dependencies — use only what's already in requirements.txt
