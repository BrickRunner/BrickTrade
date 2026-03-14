---
name: upgrade-market-intelligence-to-10
description: Upgrade Market Intelligence System to professional 10/10 quality from both trading and IT perspectives.
---

# Upgrade Market Intelligence System to 10/10

You are upgrading the Market Intelligence System in `market_intelligence/` to elite professional quality. This system analyzes crypto derivatives markets (OKX, HTX) and generates regime/opportunity reports via Telegram.

**Read ALL files in `market_intelligence/` AND `arbitrage/core/market_data.py` AND `arbitrage/exchanges/okx_rest.py`, `arbitrage/exchanges/htx_rest.py`, `arbitrage/exchanges/bybit_rest.py` AND `tests/test_market_intelligence.py` before making ANY changes.** Understand the full pipeline: collector -> feature_engine -> regime -> scorer -> portfolio -> engine -> output.

**CONSTRAINTS (non-negotiable):**
- Do NOT change the public API of `MarketIntelligenceEngine.run_once()` or `MarketIntelligenceReport`
- Do NOT change `.env` variable names or add required new env vars (new params must have sensible defaults)
- Do NOT add external dependencies — use ONLY what's in `requirements.txt` (no numpy, no pandas, no scipy)
- Keep all changes backward-compatible with existing config
- Preserve all existing logging
- All new code must pass `python -m pytest tests/test_market_intelligence.py -v`
- NEVER break existing tests. If a test fails after your change, read the test first — decide if the test or your code is wrong

---

## PHASE 1: ORDERBOOK DEPTH INTEGRATION (Critical Missing Data)

### 1.1 Add `fetch_orderbook_depth` to `MarketDataEngine`

**File:** `arbitrage/core/market_data.py`

All three exchange REST clients already have `get_orderbook()` methods:
- `OKXRestClient.get_orderbook(inst_id, sz=5)` — returns `{"data": [{"bids": [[price, qty, ...], ...], "asks": [...]}]}`
- `HTXRestClient.get_orderbook(symbol, category="linear", limit=5)` — returns `{"tick": {"bids": [[price, qty], ...], "asks": [...]}}`
- `BybitRestClient.get_orderbook(symbol, category="linear", limit=5)` — returns `{"result": {"b": [[price, qty], ...], "a": [...]}}`

Add a new method to `MarketDataEngine`:

```python
async def fetch_orderbook_depth(self, exchange: str, symbol: str, levels: int = 10) -> Optional[Dict[str, Any]]:
    """Fetch orderbook depth and return normalized {bids: [[price, qty], ...], asks: [...], timestamp: float}.
    Returns None on failure."""
```

Implementation:
- Call the appropriate `get_orderbook()` for the exchange
- Normalize the response into a unified format: `{"bids": [[float, float], ...], "asks": [[float, float], ...], "timestamp": time.time()}`
- Handle each exchange's response format separately (OKX has 4-element arrays, HTX wraps in "tick", Bybit wraps in "result")
- Convert symbol to exchange-specific format (OKX needs `BTC-USDT-SWAP`, HTX needs `BTC-USDT`, Bybit uses `BTCUSDT`)
- Use existing symbol conversion patterns already in `market_data.py` (look at `_fetch_futures_prices` for each exchange)
- Catch exceptions, log warning, return None

### 1.2 Fetch orderbook depth in collector

**File:** `market_intelligence/collector.py`

Add a new method `_update_orderbook_depth` to `MarketDataCollector`:

```python
async def _update_orderbook_depth(self, symbols: List[str]) -> None:
    """Fetch orderbook depth from all exchanges, compute imbalance per symbol."""
    for symbol in symbols:
        total_bid_vol = 0.0
        total_ask_vol = 0.0
        sources = 0
        for ex in self.exchanges:
            cb = self._circuit_breakers[ex]
            if not cb.is_available():
                continue
            limiters = get_exchange_rate_limiters()
            await limiters.get(ex).acquire()
            try:
                depth = await self.market_data.fetch_orderbook_depth(ex, symbol, levels=10)
                if depth and depth["bids"] and depth["asks"]:
                    total_bid_vol += sum(qty for _, qty in depth["bids"])
                    total_ask_vol += sum(qty for _, qty in depth["asks"])
                    sources += 1
                cb.record_success()
            except Exception as e:
                cb.record_failure()
                logger.warning("Orderbook depth %s/%s: %s", ex, symbol, e)

        if sources > 0 and (total_bid_vol + total_ask_vol) > 0:
            imbalance = (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
            self._ob_cache[symbol] = imbalance
            self._ob_bid_vol_cache[symbol] = total_bid_vol
            self._ob_ask_vol_cache[symbol] = total_ask_vol
```

Add caches `_ob_cache`, `_ob_bid_vol_cache`, `_ob_ask_vol_cache` as `Dict[str, float]` in `__init__`.

Call `_update_orderbook_depth(symbols)` inside `collect()` — add it to the `asyncio.gather` block or call it after the gather (depends on rate limiting concerns; calling after gather is safer to not overwhelm rate limits).

In the snapshot building loop, replace the hardcoded `None` for orderbook fields:
```python
ob_imbalance = self._ob_cache.get(symbol)
ob_bid_vol = self._ob_bid_vol_cache.get(symbol)
ob_ask_vol = self._ob_ask_vol_cache.get(symbol)
```

### 1.3 Use orderbook imbalance in scoring

**File:** `market_intelligence/scorer.py`

Add orderbook imbalance as a directional confirmation signal inside the `score()` method. After computing `regime_alignment_score` and before computing `raw_score`:

```python
# Orderbook imbalance confirmation/contradiction
ob_imb_raw = float(v.get("orderbook_imbalance") or 0.0) if v.get("orderbook_imbalance") is not None else None
ob_bonus = 0.0
if ob_imb_raw is not None:
    if reg.regime == MarketRegime.TREND_UP and ob_imb_raw > 0.15:
        ob_bonus = 0.08 * min(1.0, ob_imb_raw / 0.5)
        reasons.append(f"ob_confirms_up={ob_imb_raw:.2f}")
    elif reg.regime == MarketRegime.TREND_DOWN and ob_imb_raw < -0.15:
        ob_bonus = 0.08 * min(1.0, abs(ob_imb_raw) / 0.5)
        reasons.append(f"ob_confirms_down={ob_imb_raw:.2f}")
    elif reg.regime == MarketRegime.TREND_UP and ob_imb_raw < -0.2:
        ob_bonus = -0.05
        reasons.append(f"ob_contradicts_up={ob_imb_raw:.2f}")
    elif reg.regime == MarketRegime.TREND_DOWN and ob_imb_raw > 0.2:
        ob_bonus = -0.05
        reasons.append(f"ob_contradicts_down={ob_imb_raw:.2f}")
```

Add `ob_bonus` to `raw_score` calculation. Add `"orderbook_bonus": ob_bonus` to the `breakdown` dict.

---

## PHASE 2: BASIS ACCELERATION & SPREAD SMOOTHING (Noise Reduction)

### 2.1 Regression slope for basis acceleration

**File:** `market_intelligence/indicators.py`

Add a new function:

```python
def linear_slope(values: List[float], window: int = 10) -> float:
    """Least-squares linear regression slope over last `window` values.
    Normalized by mean absolute value for cross-asset comparability.
    Returns 0.0 if insufficient data."""
    if len(values) < max(3, window):
        return 0.0
    w = values[-window:]
    n = len(w)
    x_mean = (n - 1) / 2.0
    y_mean = sum(w) / n
    num = sum((i - x_mean) * (w[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den <= 1e-12:
        return 0.0
    slope = num / den
    # Normalize by mean absolute value for comparability
    abs_mean = sum(abs(v) for v in w) / n
    if abs_mean <= 1e-12:
        return 0.0
    return slope / abs_mean
```

### 2.2 Use slope instead of single-bar delta for basis/spread acceleration

**File:** `market_intelligence/feature_engine.py`

Import `linear_slope` from indicators. Replace:
```python
basis_acc = basis_hist[-1] - basis_hist[-2] if len(basis_hist) >= 2 else 0.0
spread_acc = spread_hist[-1] - spread_hist[-2] if len(spread_hist) >= 2 else 0.0
```

With:
```python
basis_acc = linear_slope(basis_hist, window=10)
spread_acc = linear_slope(spread_hist, window=10)
```

Keep the existing `basis_acc_z` and `spread_acc_z` calculations but use the new smoothed values. This removes single-bar noise while preserving the z-score normalization.

Also add smoothed funding delta using the same approach:
```python
funding_slope = linear_slope(funding_hist, window=10)
```
Add `"funding_slope": funding_slope` to the `values` dict (keep existing `funding_delta` for backward compat).

---

## PHASE 3: REGIME MODEL ENHANCEMENTS

### 3.1 Orderbook pressure in regime classification

**File:** `market_intelligence/regime.py`, method `_classify`

After the market structure block and before `probs = self._softmax(logits)`, add:

```python
# Orderbook pressure: significant imbalance reinforces trend direction
ob_imb = v.get("orderbook_imbalance")
if ob_imb is not None:
    ob_val = float(ob_imb)
    if ob_val > 0.2:
        logits[MarketRegime.TREND_UP] += 0.12 * min(1.0, ob_val / 0.5)
        logits[MarketRegime.TREND_DOWN] -= 0.06 * min(1.0, ob_val / 0.5)
    elif ob_val < -0.2:
        logits[MarketRegime.TREND_DOWN] += 0.12 * min(1.0, abs(ob_val) / 0.5)
        logits[MarketRegime.TREND_UP] -= 0.06 * min(1.0, abs(ob_val) / 0.5)
```

### 3.2 Add funding slope to regime interactions

In `_classify`, after the existing funding divergence block, add:

```python
# Funding acceleration: rapidly changing funding warns of regime shift
funding_slope_v = float(v.get("funding_slope") or 0.0)
if abs(funding_slope_v) > 0.5:
    logits[MarketRegime.HIGH_VOLATILITY] += 0.15 * min(1.0, abs(funding_slope_v))
```

### 3.3 Add liquidation cascade detection to `_is_extreme`

**File:** `market_intelligence/regime.py`, method `_is_extreme`

Add before the final `return False`:

```python
# Liquidation cascade
liq_z = float(feature.normalized.get("liquidation_cluster") or 0.0)
if liq_z >= 2.5:
    return True
# Funding extreme
funding_z = float(feature.normalized.get("funding_rate") or 0.0)
if abs(funding_z) >= 3.0:
    return True
```

---

## PHASE 4: ADAPTIVE CORRELATION WINDOWS

### 4.1 Volatility-adaptive correlation window

**File:** `market_intelligence/engine.py`, method `_correlations_to_btc`

Replace the fixed window selection:

```python
if regime and regime.regime in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
    window = self.config.stress_correlation_window
else:
    window = self.config.correlation_window
```

With a more nuanced adaptive approach:

```python
# Adaptive window: scale between stress and normal based on regime confidence
if regime and regime.regime in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
    base_window = self.config.stress_correlation_window
elif regime and regime.regime in {MarketRegime.OVERHEATED}:
    # Transitional: blend between stress and normal
    blend = min(1.0, regime.confidence)
    base_window = int(
        self.config.stress_correlation_window * blend
        + self.config.correlation_window * (1.0 - blend)
    )
else:
    base_window = self.config.correlation_window

# Further reduce window if regime is unstable (recently changed)
if regime and regime.stable_for_cycles <= 2:
    window = max(self.config.stress_correlation_window, base_window // 2)
else:
    window = base_window
```

---

## PHASE 5: SCORER IMPROVEMENTS

### 5.1 Volume-weighted spread as liquidity signal

**File:** `market_intelligence/scorer.py`

Improve `liquidity_score` by incorporating spread tightness. Replace:

```python
liquidity_score = self._clip01(
    math.log1p(volumes.get(symbol, 0.0)) / max(log_max, 1e-9)
) if log_max > 1e-9 else 0.5
```

With:

```python
# Liquidity: combine volume rank with spread tightness
vol_component = self._clip01(
    math.log1p(volumes.get(symbol, 0.0)) / max(log_max, 1e-9)
) if log_max > 1e-9 else 0.5
spread_bps = float(v.get("spread_bps") or 0.0)
spread_component = self._clip01(1.0 - min(1.0, spread_bps / 30.0))  # tighter spread = better
liquidity_score = 0.6 * vol_component + 0.4 * spread_component
```

### 5.2 Basis momentum as directional signal

Add basis slope to directional bias determination. Replace the existing bias block:

```python
funding_rate = float(v.get("funding_rate") or 0.0)
basis_bps = float(v.get("basis_bps") or 0.0)
```

With:

```python
funding_rate = float(v.get("funding_rate") or 0.0)
basis_bps = float(v.get("basis_bps") or 0.0)
basis_slope = float(v.get("basis_acceleration") or 0.0)  # now uses linear_slope
```

And update the bias logic to incorporate basis acceleration:

```python
# Directional bias: funding + basis level + basis acceleration
long_signals = 0
short_signals = 0
if funding_rate > 0: short_signals += 1
elif funding_rate < 0: long_signals += 1
if basis_bps > 5: short_signals += 1
elif basis_bps < -5: long_signals += 1
if basis_slope > 0.1: short_signals += 1  # accelerating contango = crowding
elif basis_slope < -0.1: long_signals += 1

if short_signals >= 2 and short_signals > long_signals:
    bias = "short"
elif long_signals >= 2 and long_signals > short_signals:
    bias = "long"
else:
    bias = "neutral"
```

### 5.3 Add funding_slope to directional signals dict

Add to the `directional_signals` dict:
```python
"funding_momentum": "accelerating" if float(v.get("funding_slope") or 0.0) > 0.3 else "decelerating" if float(v.get("funding_slope") or 0.0) < -0.3 else "stable",
```

---

## PHASE 6: PORTFOLIO ANALYZER ENHANCEMENTS

### 6.1 Correlation-aware diversification penalty

**File:** `market_intelligence/portfolio.py`, method `analyze`

After the cap redistribution block, add cross-pair correlation penalty. When two allocated pairs have high correlation to BTC (both > 0.8), reduce the smaller allocation:

```python
# Correlation-aware concentration penalty
corr_threshold = 0.8
high_corr_pairs = [s for s in allocation if abs(correlations_to_btc.get(s, 0.0)) > corr_threshold]
if len(high_corr_pairs) > 2:
    # Too many highly-correlated pairs — penalize the weakest
    sorted_hc = sorted(high_corr_pairs, key=lambda s: allocation[s], reverse=True)
    for s in sorted_hc[2:]:  # keep top 2, penalize rest
        allocation[s] *= 0.7
    # Re-normalize to 100%
    total_rebalanced = sum(allocation.values())
    if total_rebalanced > 0:
        for s in allocation:
            allocation[s] = 100.0 * allocation[s] / total_rebalanced
```

### 6.2 Dynamic exposure cap based on regime

Replace the hardcoded `recommended_exposure_cap_pct`:

```python
# Dynamic exposure cap
if global_regime.regime in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
    exposure_cap = 15.0
elif global_regime.regime == MarketRegime.OVERHEATED:
    exposure_cap = 30.0
elif not aggressive_mode_enabled:
    exposure_cap = 50.0
else:
    exposure_cap = 100.0

# Further reduce if data health is degraded
if data_health_status == DataHealthStatus.PARTIAL:
    exposure_cap = min(exposure_cap, 40.0)
elif data_health_status == DataHealthStatus.INVALID:
    exposure_cap = 10.0
```

Use `exposure_cap` in the returned `PortfolioRiskSignal.recommended_exposure_cap_pct`.

---

## PHASE 7: OUTPUT IMPROVEMENTS

### 7.1 Add orderbook and funding momentum to human report

**File:** `market_intelligence/output.py`, function `format_human_report`

In the "УСЛОВИЯ ТОРГОВЛИ" section, after breakout probability, add:

```python
# Orderbook pressure (BTC)
btc_features = payload.get("features", {}).get(btc_sym, {})
ob_imb = btc_features.get("orderbook_imbalance")
if ob_imb is not None:
    ob_val = float(ob_imb)
    if ob_val > 0.15:
        lines.append(f"Давление стакана: покупатели ({ob_val:+.2f})")
    elif ob_val < -0.15:
        lines.append(f"Давление стакана: продавцы ({ob_val:+.2f})")
    else:
        lines.append(f"Давление стакана: нейтральное ({ob_val:+.2f})")
```

### 7.2 Add directional bias to opportunity display

In the opportunities section, improve the display of top opportunities:

```python
lines.append("Лучшие идеи: " + ", ".join(
    f"{x.symbol} ({x.score:.0f}/100, {_bias_ru(x.directional_bias)}, уверенность {x.confidence:.0%})" for x in shown
))
```

Add helper:
```python
def _bias_ru(bias: str) -> str:
    return {"short": "шорт", "long": "лонг", "neutral": "нейтрально"}.get(bias, bias)
```

### 7.3 Enrich contextual notes

**File:** `market_intelligence/output.py`, function `_contextual_notes`

Add new contextual observations:

```python
# Orderbook divergence from price trend
ob_imb = (payload.get("features", {}).get(btc_sym, {}) or {}).get("orderbook_imbalance")
if ob_imb is not None and regime_name == "trend_up" and float(ob_imb) < -0.25:
    notes.append("Стакан расходится с трендом: давление продавцов при восходящем движении.")
if ob_imb is not None and regime_name == "trend_down" and float(ob_imb) > 0.25:
    notes.append("Стакан расходится с трендом: давление покупателей при нисходящем движении.")

# Basis acceleration warning
basis_acc = (payload.get("features", {}).get(btc_sym, {}) or {}).get("basis_acceleration")
if basis_acc is not None and abs(float(basis_acc)) > 0.5:
    if float(basis_acc) > 0:
        notes.append("Ускоренный рост базиса — возможно нарастание спекулятивных позиций.")
    else:
        notes.append("Резкое сжатие базиса — возможна ликвидация или деливеридж.")
```

---

## PHASE 8: HEALTH CHECK & MONITORING

### 8.1 Add lightweight health check to service

**File:** `market_intelligence/service.py`

Add method to `MarketIntelligenceService`:

```python
async def health_check(self) -> Dict[str, Any]:
    """Lightweight health check without running a full cycle."""
    result = {
        "initialized": self._engine is not None,
        "last_report_age_seconds": None,
        "last_report_status": None,
        "exchanges": [],
        "symbols_count": 0,
    }
    if self._cfg:
        result["exchanges"] = self._cfg.exchanges
        result["symbols_count"] = len(self._cfg.symbols)
    if self._last_report:
        result["last_report_age_seconds"] = time.time() - self._last_report_ts
        result["last_report_status"] = self._last_report.payload.get("status")
    if self._collector:
        breaker_status = {}
        for ex, cb in self._collector._circuit_breakers.items():
            breaker_status[ex] = {
                "available": cb.is_available(),
                "failure_count": cb.failure_count,
            }
        result["circuit_breakers"] = breaker_status
    return result
```

### 8.2 Add health check to integration

**File:** `market_intelligence/integration.py`

Add:

```python
async def market_intelligence_health() -> Dict[str, Any]:
    service = get_market_intelligence_service()
    return await service.health_check()
```

---

## PHASE 9: COMPREHENSIVE TESTS

**File:** `tests/test_market_intelligence.py`

Add the following new tests AT THE END of the file (do not modify existing tests):

```python
def test_linear_slope_basic():
    """Linear slope detects positive/negative/flat trends."""
    from market_intelligence.indicators import linear_slope
    rising = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert linear_slope(rising, 10) > 0.1
    falling = list(reversed(rising))
    assert linear_slope(falling, 10) < -0.1
    flat = [5.0] * 10
    assert abs(linear_slope(flat, 10)) < 1e-9
    assert linear_slope([1.0, 2.0], 10) == 0.0  # insufficient data


def test_linear_slope_vs_single_bar_delta():
    """Slope is more robust than single-bar delta to noise."""
    from market_intelligence.indicators import linear_slope
    # Steady uptrend with one noisy bar
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 3.0]  # last bar is noise
    slope = linear_slope(values, 10)
    single_bar = values[-1] - values[-2]  # = -6.0, very misleading
    # Slope should still be positive (uptrend), single bar is very negative
    assert slope > 0, f"Slope should detect uptrend despite noisy bar: {slope}"
    assert single_bar < -5.0, "Single bar delta is misleading"


def test_orderbook_imbalance_in_snapshot():
    """Snapshot correctly stores orderbook imbalance values."""
    snap = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=99.9, ask=100.1, spot_price=99.95,
        funding_rate=0.0001,
        orderbook_imbalance=0.35,
        orderbook_bid_volume=1500.0,
        orderbook_ask_volume=850.0,
    )
    assert snap.orderbook_imbalance == 0.35
    assert snap.orderbook_bid_volume == 1500.0
    assert snap.orderbook_ask_volume == 850.0


def test_regime_extreme_bypass_with_liquidation():
    """Extreme liquidation z-score should trigger fast-path bypass."""
    model = RegimeModel(confidence_threshold=0.55, min_duration_cycles=3, smoothing_alpha=0.35)
    extreme_vals = {
        "ema_cross": -2.0, "adx": 40.0, "rsi": 20.0,
        "funding_rate": -0.001, "liquidation_cluster": 3.0,
        "rolling_volatility": 0.05, "bb_width": 0.1,
        "volume_spike": 5.0, "volume_trend": 2.0,
        "market_structure_code": -1.0, "atr_percentile": 0.9,
        "orderbook_imbalance": -0.4, "funding_slope": -0.5,
    }
    extreme_z = dict(extreme_vals)
    extreme_z["liquidation_cluster"] = 3.0  # z-score above 2.5
    fv = FeatureVector("BTCUSDT", time.time(), extreme_vals, extreme_z)
    assert model._is_extreme(fv) is True


def test_portfolio_max_allocation_cap():
    """No single pair should exceed 25% allocation."""
    from market_intelligence.portfolio import PortfolioAnalyzer
    from market_intelligence.models import RegimeState, OpportunityScore, DataHealthStatus
    analyzer = PortfolioAnalyzer()
    # Create opportunities with very skewed scores
    opportunities = [
        OpportunityScore("BTCUSDT", 95.0, 0.9, MarketRegime.TREND_UP, [], {}, "long"),
        OpportunityScore("ETHUSDT", 20.0, 0.4, MarketRegime.RANGE, [], {}, "neutral"),
        OpportunityScore("SOLUSDT", 15.0, 0.3, MarketRegime.RANGE, [], {}, "neutral"),
    ]
    global_regime = RegimeState(MarketRegime.TREND_UP, 0.8, {MarketRegime.TREND_UP: 0.8}, 5)
    local_regimes = {
        "BTCUSDT": global_regime,
        "ETHUSDT": RegimeState(MarketRegime.RANGE, 0.6, {}, 5),
        "SOLUSDT": RegimeState(MarketRegime.RANGE, 0.5, {}, 5),
    }
    result = analyzer.analyze(
        opportunities, local_regimes,
        {"BTCUSDT": 1.0, "ETHUSDT": 0.8, "SOLUSDT": 0.6},
        global_regime, global_atr_pct=1.5, global_volatility_regime="medium",
        data_health_status=DataHealthStatus.OK, scoring_enabled=True,
    )
    for symbol, pct in result.capital_allocation_pct.items():
        assert pct <= 25.01, f"{symbol} allocation {pct}% exceeds 25% cap"


def test_portfolio_exposure_cap_by_regime():
    """Exposure cap should be lower during PANIC than TREND_UP."""
    from market_intelligence.portfolio import PortfolioAnalyzer
    from market_intelligence.models import RegimeState, OpportunityScore, DataHealthStatus
    analyzer = PortfolioAnalyzer()
    opportunities = [
        OpportunityScore("BTCUSDT", 60.0, 0.7, MarketRegime.PANIC, [], {}, "short"),
    ]
    panic_regime = RegimeState(MarketRegime.PANIC, 0.8, {MarketRegime.PANIC: 0.8}, 5)
    result_panic = analyzer.analyze(
        opportunities, {"BTCUSDT": panic_regime},
        {"BTCUSDT": 1.0}, panic_regime,
        global_atr_pct=3.0, global_volatility_regime="high",
        data_health_status=DataHealthStatus.OK, scoring_enabled=True,
    )
    trend_regime = RegimeState(MarketRegime.TREND_UP, 0.8, {MarketRegime.TREND_UP: 0.8}, 5)
    result_trend = analyzer.analyze(
        opportunities, {"BTCUSDT": trend_regime},
        {"BTCUSDT": 1.0}, trend_regime,
        global_atr_pct=1.0, global_volatility_regime="medium",
        data_health_status=DataHealthStatus.OK, scoring_enabled=True,
    )
    assert result_panic.recommended_exposure_cap_pct < result_trend.recommended_exposure_cap_pct


def test_scorer_liquidity_uses_spread():
    """Liquidity score should incorporate spread tightness, not just volume."""
    from market_intelligence.models import RegimeState
    scorer = OpportunityScorer()
    base_vals = {
        "rolling_volatility_local": 0.01, "bb_width_local": 0.02,
        "funding_rate": 0.0003, "funding_delta": 0.0001,
        "oi_delta": 100.0, "oi_delta_pct": 5.0,
        "rolling_volatility": 0.005, "volume_proxy": 1000.0,
        "basis_bps": 10.0, "funding_pct": 0.03,
        "macd_hist": 0.01, "volume_spike": 1.2, "cvd": 0.1,
        "data_quality_code": 0.0, "basis_acceleration": 0.0,
        "funding_slope": 0.0, "orderbook_imbalance": None,
    }
    base_z = {
        "rolling_volatility_local": 0.5, "bb_width_local": 0.3,
        "funding_rate": 0.4, "funding_delta": 0.2,
        "oi_delta": 0.6, "rolling_volatility": 0.3,
    }
    # Tight spread
    vals_tight = dict(base_vals, spread_bps=2.0)
    fv_tight = FeatureVector("BTCUSDT", time.time(), vals_tight, base_z)
    # Wide spread
    vals_wide = dict(base_vals, spread_bps=50.0)
    fv_wide = FeatureVector("BTCUSDT", time.time(), vals_wide, base_z)

    reg = RegimeState(MarketRegime.TREND_UP, 0.7, {MarketRegime.TREND_UP: 0.7}, 5)
    scores_tight = scorer.score(
        {"BTCUSDT": fv_tight}, {"BTCUSDT": reg},
        {"BTCUSDT": 0.5}, {"BTCUSDT": 0.5},
    )
    scores_wide = scorer.score(
        {"BTCUSDT": fv_wide}, {"BTCUSDT": reg},
        {"BTCUSDT": 0.5}, {"BTCUSDT": 0.5},
    )
    # Tight spread should score higher
    assert scores_tight[0].score >= scores_wide[0].score


def test_health_check():
    """Health check returns expected structure."""
    import asyncio
    from market_intelligence.service import MarketIntelligenceService
    service = MarketIntelligenceService()
    result = asyncio.get_event_loop().run_until_complete(service.health_check())
    assert result["initialized"] is False
    assert result["last_report_age_seconds"] is None
    assert "exchanges" in result
```

---

## PHASE 10: FINAL VALIDATION

After all changes, run:

```bash
python -m pytest tests/test_market_intelligence.py -v
```

All tests (old AND new) must pass. If any test fails:
1. Read the failing test
2. Determine if the test expectation is wrong or your code is wrong
3. Fix the actual bug, do not blindly adjust test expectations
4. Re-run tests

Then verify code consistency:
- Every new field added to `values` dict in `feature_engine.py` must also be added to `normalized` dict (via z-score or explicit)
- Every new indicator used in `regime.py` must gracefully handle `None` with `or 0.0` fallback
- Every new `reasons.append()` in `scorer.py` must correspond to a real computed value
- `_assert_consistency` in `engine.py` should not need changes (new fields are optional)

---

## SUMMARY OF ALL FILES TO MODIFY

1. `arbitrage/core/market_data.py` — add `fetch_orderbook_depth()`
2. `market_intelligence/indicators.py` — add `linear_slope()`
3. `market_intelligence/collector.py` — add orderbook depth fetching, caches
4. `market_intelligence/feature_engine.py` — use `linear_slope` for basis/spread/funding, import changes
5. `market_intelligence/regime.py` — add orderbook pressure, funding slope, liquidation cascade in `_is_extreme`
6. `market_intelligence/scorer.py` — add orderbook confirmation, improve liquidity, improve directional bias
7. `market_intelligence/portfolio.py` — correlation diversification penalty, dynamic exposure cap
8. `market_intelligence/output.py` — orderbook display, bias translation, enriched notes
9. `market_intelligence/service.py` — health check method
10. `market_intelligence/integration.py` — health check export
11. `tests/test_market_intelligence.py` — add 8+ new tests

## FILES TO READ BUT NOT MODIFY
- `market_intelligence/models.py` — reference for data structures (PairSnapshot already has orderbook fields)
- `market_intelligence/config.py` — reference for config (no new required env vars)
- `market_intelligence/engine.py` — only modify `_correlations_to_btc` adaptive window
- `market_intelligence/statistics.py` — no changes needed
- `market_intelligence/validation.py` — no changes needed
- `market_intelligence/persistence.py` — no changes needed (orderbook cache is transient, not persisted)
- `market_intelligence/rate_limiter.py` — no changes needed
