---
name: perfect-market-intelligence
description: "Исправить ВСЕ баги, интегрировать мёртвый код, починить data pipeline, стабилизировать scoring — довести Market Intelligence до 10/10."
---

# Market Intelligence System — Fix Everything to 10/10

Ты выполняешь полную переработку Market Intelligence System по результатам профессионального аудита. Каждый пункт ниже — конкретный баг или недочёт с точным расположением в коде и точным описанием исправления.

---

## ОБЯЗАТЕЛЬНАЯ ПОДГОТОВКА

**Прочитай ВСЕ файлы перед началом работы:**

```
market_intelligence/__init__.py
market_intelligence/config.py
market_intelligence/models.py
market_intelligence/collector.py
market_intelligence/feature_engine.py
market_intelligence/indicators.py
market_intelligence/statistics.py
market_intelligence/regime.py
market_intelligence/scorer.py
market_intelligence/portfolio.py
market_intelligence/ml_weights.py
market_intelligence/order_flow.py
market_intelligence/engine.py
market_intelligence/service.py
market_intelligence/output.py
market_intelligence/validation.py
market_intelligence/persistence.py
market_intelligence/rate_limiter.py
market_intelligence/logger.py
market_intelligence/metrics.py
market_intelligence/protocols.py
market_intelligence/integration.py
arbitrage/core/market_data.py
tests/test_market_intelligence.py
```

**ЖЁСТКИЕ ОГРАНИЧЕНИЯ:**
- НЕ менять публичное API `MarketIntelligenceEngine.run_once()` и `MarketIntelligenceReport`
- НЕ менять имена .env переменных, НЕ добавлять обязательные новые env vars — всё с дефолтами
- НЕ добавлять внешние зависимости (no numpy, no pandas, no scipy, no sklearn)
- Обратная совместимость с существующей конфигурацией
- Все тесты должны проходить: `python -m pytest tests/ -v`
- Если тест ломается — сначала прочитай тест, пойми что он проверяет, потом реши что менять
- Type hints везде, `from __future__ import annotations`
- Следуй стилю проекта (dataclasses, `or 0.0` fallbacks)

**ПОРЯДОК РАБОТЫ:** Выполняй блоки строго последовательно. После каждого блока запускай тесты. Не переходи к следующему пока текущий не работает.

---

## БЛОК A: КРИТИЧЕСКИЕ БАГИ DATA PIPELINE

### A1. Починить volume-weighted aggregation в collector.py

**Файл:** `market_intelligence/collector.py`, метод `collect()`, строки ~258-293

**Баг:** `ex_volumes` берёт `self._volume_cache.get(symbol)` для КАЖДОЙ биржи, но `_update_24h_volume()` агрегирует volume по символу (суммирует все биржи в одно число). Результат: все биржи получают одинаковый volume, и "volume-weighted average" фактически равен simple average.

**Исправление:**

1. В `_update_24h_volume()` сохраняй per-exchange volumes:
```python
async def _update_24h_volume(self) -> None:
    """Fetch real 24h volume from all exchanges, store per-exchange and aggregated."""
    merged: Dict[str, float] = {}
    per_exchange: Dict[str, Dict[str, float]] = {}  # {exchange: {symbol: volume}}
    for ex in self.exchanges:
        cb = self._circuit_breakers[ex]
        if not cb.is_available():
            continue
        try:
            vol_data = await self.market_data.fetch_24h_volumes(ex)
            per_exchange[ex] = vol_data
            for sym, val in vol_data.items():
                merged[sym] = merged.get(sym, 0.0) + val
            cb.record_success()
        except Exception as e:
            cb.record_failure()
            logger.warning("Volume fetch %s: %s", ex, e)
    self._volume_cache.update(merged)
    self._volume_per_exchange_cache = per_exchange
```

2. Добавь `self._volume_per_exchange_cache: Dict[str, Dict[str, float]] = {}` в `__init__`

3. В `collect()` замени:
```python
# БЫЛО (broken):
ex_volumes = {ex: self._volume_cache.get(symbol, 0.0) for ex in ex_prices}

# СТАЛО (correct):
ex_volumes = {}
for ex in ex_prices:
    ex_vol = self._volume_per_exchange_cache.get(ex, {}).get(symbol, 0.0)
    ex_volumes[ex] = ex_vol
```

### A2. Исправить bid/ask как proxy для high/low

**Файл:** `market_intelligence/feature_engine.py`, метод `compute()`, строки ~41-43

**Баг:** `highs = h.get("ask", closes)` и `lows = h.get("bid", closes)` — bid/ask spread это НЕ high/low range. ATR, ADX, Bollinger Bands получают систематически заниженные значения.

**Исправление:**

1. Всегда предпочитать OHLCV candle данные для ATR/ADX/BB/market structure. Использовать bid/ask только как last resort.

2. Реструктурировать начало метода `compute()`:
```python
for symbol, snap in snapshots.items():
    h = histories.get(symbol, {})
    closes = h.get("price", [])

    # Primary: OHLCV candle data (accurate high/low)
    ohlcv_1h = (candles or {}).get(symbol, {}).get("1H", [])
    if ohlcv_1h and len(ohlcv_1h) >= 30:
        candle_closes = [c.close for c in ohlcv_1h]
        candle_highs = [c.high for c in ohlcv_1h]
        candle_lows = [c.low for c in ohlcv_1h]
        candle_vols = [c.volume for c in ohlcv_1h]
        # Use candle data for ALL indicators that need HLC
        highs_for_indicators = candle_highs
        lows_for_indicators = candle_lows
        closes_for_indicators = candle_closes
        volumes_for_indicators = candle_vols
        using_candles = True
    else:
        # Fallback: bid/ask as proxy (mark as degraded)
        highs_for_indicators = h.get("ask", closes)
        lows_for_indicators = h.get("bid", closes)
        closes_for_indicators = closes
        volumes_for_indicators = h.get("volume", [])
        using_candles = False
```

3. Использовать `highs_for_indicators`, `lows_for_indicators`, `closes_for_indicators` для вычисления ATR, ADX, BB, market structure, VWAP. Добавить фичу `"using_candle_data": 1.0 if using_candles else 0.0` в values dict.

4. Когда `using_candles = False`, пометить ATR/ADX пониженным confidence — уже сделано через `atr_proxy_penalty_applied`, но убедись что эта пометка действительно используется в scorer.

### A3. Исправить двойной push atr_pct в RollingStats

**Файл:** `market_intelligence/feature_engine.py`

**Баг:** `atr_pct` пушится в RollingStats дважды — один раз в общем цикле нормализации (строка ~260, цикл `for key, val in values.items()`) и второй раз явно (строки ~288-291 `atr_stats = self._stats[symbol].setdefault("atr_pct", ...); atr_stats.push(atr_pct)`).

**Исправление:** Удалить явный `atr_stats.push(atr_pct)` после цикла нормализации. Percentile rank вычислять на stats объекте, который уже существует после цикла:
```python
# ПОСЛЕ цикла нормализации
atr_stats = self._stats[symbol].get("atr_pct")
if atr_stats:
    atr_percentile = atr_stats.percentile_rank(atr_pct if atr_pct is not None else 0.0)
else:
    atr_percentile = 0.5
```

---

## БЛОК B: ИНТЕГРАЦИЯ МЁРТВОГО КОДА

### B1. Интегрировать OrderFlowAnalyzer

**Файл:** `market_intelligence/order_flow.py` — полностью написан, но нигде не используется.

**Интеграция:**

1. В `market_intelligence/engine.py`, добавить `OrderFlowAnalyzer` как опциональный компонент:
```python
from market_intelligence.order_flow import OrderFlowAnalyzer

class MarketIntelligenceEngine:
    def __init__(self, config, collector):
        ...
        self.order_flow: Optional[OrderFlowAnalyzer] = None
        if config.order_flow_enabled:
            self.order_flow = OrderFlowAnalyzer()
```

2. В `config.py` добавить:
```python
order_flow_enabled: bool  # default False
```
В `from_env()`:
```python
order_flow_enabled=_as_bool("MI_ORDER_FLOW_ENABLED", False),
```

3. В `_compute_pipeline()`, если `self.order_flow` доступен, добавить flow features к feature vectors:
```python
if self.order_flow:
    for symbol, fv in features.items():
        flow = self.order_flow.get_flow_features(symbol)
        fv.values.update(flow)
```

4. В `scorer.py`, использовать `flow_delta_ratio` и `flow_absorption_score` если доступны:
- `flow_delta_ratio > 0.3` при TREND_UP → boost regime_alignment_score на +0.05
- `flow_absorption_score > 0.5` → boost liquidity_score на +0.05
- `flow_delta_divergence == 1.0` → добавить risk warning в reasons

5. Добавить order flow данные в payload отчёта.

### B2. Интегрировать funding_zscore_adaptive

**Файл:** `market_intelligence/indicators.py` — функция `funding_zscore_adaptive` написана, но не вызывается.

**Интеграция в `feature_engine.py`:**

```python
from market_intelligence.indicators import funding_zscore_adaptive

# В compute(), после вычисления funding_delta:
funding_hist_list = h.get("funding", [])
funding_analysis = funding_zscore_adaptive(funding_hist_list, short_window=12, long_window=72)

values["funding_deviation"] = funding_analysis["funding_deviation"]
values["funding_regime_code"] = funding_analysis["funding_regime"]
values["funding_mean_reversion_signal"] = funding_analysis["funding_mean_reversion_signal"]
values["funding_acceleration_indicator"] = funding_analysis["funding_acceleration"]
values["funding_extreme_flag"] = funding_analysis["funding_extreme"]
```

**Интеграция в `scorer.py`:**
- Если `funding_extreme_flag == 1.0` → boost `funding_divergence_score` на 20%
- Если `funding_mean_reversion_signal > 0.5` → добавить reason "funding_mean_reversion"
- Использовать `funding_deviation` в directional bias calculation

### B3. Интегрировать liquidation_cascade_risk

**Файл:** `market_intelligence/indicators.py` — функция написана, не вызывается.

**Интеграция в `feature_engine.py`:**

```python
from market_intelligence.indicators import liquidation_cascade_risk

# В compute():
liq_hist = h.get("liquidation", [])
price_changes = rolling_returns(closes)
oi_deltas_hist = []
oi_hist_raw = h.get("oi", [])
for i in range(1, len(oi_hist_raw)):
    prev = oi_hist_raw[i-1]
    if abs(prev) > 1e-9:
        oi_deltas_hist.append((oi_hist_raw[i] - prev) / prev)
    else:
        oi_deltas_hist.append(0.0)

cascade = liquidation_cascade_risk(liq_hist, price_changes, oi_deltas_hist)
values["cascade_risk"] = cascade["cascade_risk"]
values["cascade_stage"] = cascade["cascade_stage"]
values["cascade_direction"] = cascade["cascade_direction"]
```

**Интеграция в `regime.py`:**
- Если `cascade_risk > 0.5` → boost PANIC logit на +0.3
- Если `cascade_stage >= 2` → fast-path extreme detection

**Интеграция в `engine.py` alerts:**
```python
cascade_risk = float(fv.values.get("cascade_risk") or 0.0)
cascade_stage = float(fv.values.get("cascade_stage") or 0.0)
if cascade_stage >= 2:
    alerts.append(f"Liquidation cascade developing: {symbol} (risk={cascade_risk:.2f}, stage={int(cascade_stage)})")
if cascade_stage >= 3:
    alerts.append(f"ACTIVE CASCADE: {symbol} — immediate risk reduction recommended")
```

### B4. Интегрировать spread_dynamics и estimate_market_impact

**Файл:** `market_intelligence/indicators.py`

**Интеграция в `feature_engine.py`:**

```python
from market_intelligence.indicators import spread_dynamics, estimate_market_impact

# spread dynamics
spread_hist_values = h.get("spread", [])
sp_dyn = spread_dynamics(spread_hist_values, window=20)
values["spread_regime_code"] = sp_dyn["spread_regime_code"]
values["spread_expansion_rate"] = sp_dyn["spread_expansion_rate"]
values["spread_percentile"] = sp_dyn["spread_percentile"]
values["liquidity_withdrawal"] = sp_dyn["liquidity_withdrawal"]

# market impact estimation
ob_bid_vol = float(snap.orderbook_bid_volume or 0.0) if hasattr(snap, 'orderbook_bid_volume') and snap.orderbook_bid_volume else 0.0
ob_ask_vol = float(snap.orderbook_ask_volume or 0.0) if hasattr(snap, 'orderbook_ask_volume') and snap.orderbook_ask_volume else 0.0
avg_trade_vol = float(snap.volume_proxy or 0.0) if snap.volume_proxy else 0.0
current_spread = float(values.get("spread_bps") or 0.0)

if ob_bid_vol > 0 and ob_ask_vol > 0 and avg_trade_vol > 0:
    impact = estimate_market_impact(ob_bid_vol, ob_ask_vol, avg_trade_vol, current_spread)
    values["market_impact_total_bps"] = impact["total_cost_bps"]
    values["entry_feasibility"] = impact["entry_feasibility"]
else:
    values["market_impact_total_bps"] = None
    values["entry_feasibility"] = None
```

**Интеграция в `scorer.py`:**
- Если `liquidity_withdrawal > 0.5` → добавить risk warning и boost risk_penalty на +0.1
- Если `entry_feasibility < 0.3` → добавить reason "low_entry_feasibility" и dampen score на 20%
- `spread_regime_code == 2` (extreme spread) → boost risk_penalty на +0.15

---

## БЛОК C: СТАБИЛИЗАЦИЯ SCORING

### C1. Исправить нормализацию score для любого числа символов

**Файл:** `market_intelligence/scorer.py`, метод `score()`, строки ~252-282

**Баг:** При 1 символе: `normalized_score = (raw_score + 0.10) * 100.0` — совершенно другая шкала чем при N>1 символах. Score нестабилен.

**Исправление:** Единая формула для любого N:

```python
# Заменить блок нормализации:
if len(raw_rows) == 0:
    return []

if len(raw_rows) == 1:
    symbol, reg, raw_score, risk_penalty, breakdown, reasons, bias, bias_strength = raw_rows[0]
    # For single symbol: use absolute signal quality as primary metric
    signal_quality = (
        breakdown["volatility_expansion_score"]
        + breakdown["funding_divergence_score"]
        + breakdown["oi_acceleration_score"]
        + breakdown["regime_alignment_score"]
    ) / 4.0
    normalized_score = self._clip(0.0, 100.0, signal_quality * 100.0)
else:
    scores = [x[2] for x in raw_rows]
    score_min = min(scores)
    score_max = max(scores)
    denom = max(score_max - score_min, 1e-9)

    ranked = sorted(raw_rows, key=lambda x: x[2], reverse=True)
    rank_index = {row[0]: idx for idx, row in enumerate(ranked)}
```

Для N>1 оставить текущую формулу (rank + minmax + signal_quality), но обрабатывать N=1 через signal_quality напрямую.

### C2. Подключить record_outcome для ML weights

**Файл:** `market_intelligence/engine.py`

**Проблема:** `scorer.record_outcome()` нигде не вызывается — ML weights никогда не обучаются.

**Исправление:** Добавить feedback loop в `run_once()`. После получения отчёта, сравнить предыдущие predictions с текущей реальностью:

```python
# В run_once(), после вычисления p = _compute_pipeline():
if self._previous_payload and self.config.adaptive_ml_weighting:
    self._record_ml_feedback(p)
```

```python
def _record_ml_feedback(self, current: PipelineResult) -> None:
    """Record outcome feedback for ML weight optimization."""
    prev_opps = self._previous_payload.get("opportunities", [])
    if not prev_opps:
        return

    for prev_opp in prev_opps:
        symbol = prev_opp["symbol"]
        prev_score = prev_opp["score"]
        prev_bias = prev_opp.get("directional_bias", "neutral")

        # Check if we have current price data
        if symbol not in current.features:
            continue

        current_price = current.features[symbol].values.get("price")
        prev_features = self._previous_payload.get("features", {}).get(symbol, {})
        prev_price = prev_features.get("price")

        if current_price is None or prev_price is None or prev_price == 0:
            continue

        # Actual outcome: price return since last cycle
        price_return = (current_price - prev_price) / prev_price

        # Map to outcome relative to predicted bias
        if prev_bias == "long":
            actual_outcome = price_return  # positive return = correct prediction
        elif prev_bias == "short":
            actual_outcome = -price_return  # negative return = correct prediction
        else:
            actual_outcome = abs(price_return)  # any movement = opportunity existed

        # Extract feature breakdown for ML
        breakdown = prev_opp.get("breakdown", {})
        if breakdown:
            self.scorer.record_outcome(
                symbol=symbol,
                score=prev_score,
                actual_outcome=actual_outcome,
                timestamp=current.features[symbol].timestamp,
                feature_vector=breakdown,
            )
```

### C3. Нормализовать сумму весов в scorer

**Файл:** `market_intelligence/scorer.py`

**Баг:** Веса `w_volatility + w_funding + w_oi + w_regime + w_liquidity` складываются, а `w_risk_penalty` вычитается. Сумма положительных весов ≠ 1.0 (0.26 + 0.24 + 0.20 + 0.30 + 0.15 = 1.15). Risk penalty (0.28) может сделать score отрицательным.

**Исправление:** Нормализовать положительные веса к 1.0 перед применением:

```python
# В score(), после определения весов (ML или regime-based):
positive_sum = w_volatility + w_funding + w_oi + w_regime + w_liquidity
if positive_sum > 0:
    w_volatility /= positive_sum
    w_funding /= positive_sum
    w_oi /= positive_sum
    w_regime /= positive_sum
    w_liquidity /= positive_sum
# risk_penalty остаётся как есть — это штраф, а не часть суммы
```

---

## БЛОК D: PORTFOLIO/RISK UPGRADE

### D1. Полноценный Historical VaR

**Файл:** `market_intelligence/portfolio.py`

**Проблема:** Текущий "VaR" — это percentile z-score волатильности. Это не VaR.

**Исправление:** Заменить tail risk секцию на proper Historical VaR:

```python
# BLOCK: Tail risk — Historical VaR (95th percentile of losses)
if features:
    # Collect recent returns for each allocated symbol
    all_weighted_returns: List[float] = []
    for sym in allocation:
        if sym in features:
            fv = features[sym]
            vol_z = fv.normalized.get("rolling_volatility")
            # Use rolling volatility as proxy for expected daily return distribution
            # In a full system, you'd have actual return series
            if vol_z is not None:
                vol = float(vol_z)
                # Approximate loss at 95th percentile: ~1.65 * sigma
                expected_loss = 1.65 * abs(vol)
                weight = allocation[sym] / 100.0
                all_weighted_returns.append(expected_loss * weight)

    if all_weighted_returns:
        portfolio_var_95 = sum(all_weighted_returns)
        # If portfolio VaR exceeds threshold, reduce allocations proportionally
        VAR_THRESHOLD = 2.0  # 2 sigma portfolio risk
        if portfolio_var_95 > VAR_THRESHOLD:
            scale_factor = VAR_THRESHOLD / portfolio_var_95
            for sym in allocation:
                allocation[sym] *= scale_factor
            # Renormalize
            total_after_var = sum(allocation.values())
            if total_after_var > 0:
                for s in allocation:
                    allocation[s] = 100.0 * allocation[s] / total_after_var
```

### D2. Починить _extract_base_currency

**Файл:** `market_intelligence/portfolio.py`

**Баг:** Ломается на `1000PEPEUSDT`, `1000SHIBUSDT`, `BTCDOMUSDT` и подобных.

**Исправление:**
```python
@staticmethod
def _extract_base_currency(symbol: str) -> str:
    """Extract base currency from trading pair symbol.

    Handles: ETHUSDT, BTCUSDC, 1000PEPEUSDT, 1000SHIBUSDT, BTCDOMUSDT
    """
    symbol_upper = symbol.upper()
    quote_currencies = ["USDT", "USDC", "USD", "BUSD", "TUSD", "DAI"]

    for quote in quote_currencies:
        if symbol_upper.endswith(quote):
            base = symbol_upper[: -len(quote)]
            # Strip numeric multiplier prefix (1000, 10000, etc.)
            stripped = base.lstrip("0123456789")
            return stripped if stripped else base

    # Fallback
    if len(symbol_upper) > 4:
        return symbol_upper[:-4]
    return symbol_upper
```

### D3. Добавить cross-correlation matrix

**Файл:** `market_intelligence/portfolio.py`

Сейчас учитывается только correlation to BTC. Добавить pairwise correlation penalty:

```python
def _pairwise_correlation_penalty(
    self,
    symbols: List[str],
    correlations_to_btc: Dict[str, float],
    allocation: Dict[str, float],
) -> Dict[str, float]:
    """Reduce allocation for symbols that are highly correlated with each other.

    Approximation: if two symbols both have high BTC correlation (>0.8),
    they are likely correlated with each other. Reduce allocation for the lower-scored one.
    """
    high_corr_symbols = [s for s in symbols if abs(correlations_to_btc.get(s, 0.0)) > 0.8]

    if len(high_corr_symbols) <= 2:
        return allocation

    # Keep top 2 by allocation, penalize the rest
    sorted_hc = sorted(high_corr_symbols, key=lambda s: allocation.get(s, 0.0), reverse=True)
    for s in sorted_hc[2:]:
        allocation[s] *= 0.7  # 30% penalty for correlated overflow

    # Renormalize
    total = sum(allocation.values())
    if total > 0:
        for s in allocation:
            allocation[s] = 100.0 * allocation[s] / total

    return allocation
```

Вызвать после основного allocation и перед cap:
```python
allocation = self._pairwise_correlation_penalty(
    list(allocation.keys()), correlations_to_btc, allocation
)
```

---

## БЛОК E: REGIME MODEL REFINEMENT

### E1. Добавить cascade risk в extreme detection

**Файл:** `market_intelligence/regime.py`, метод `_is_extreme()`

Расширить список extreme conditions:
```python
def _is_extreme(self, feature: FeatureVector | None) -> bool:
    if feature is None:
        return False
    v = feature.values
    rsi_v = float(v.get("rsi") or 50.0)
    vol_spike_v = float(v.get("volume_spike") or 1.0)
    vol_z = float(v.get("rolling_volatility") or 0.0)

    if rsi_v <= 15.0 or rsi_v >= 85.0:
        return True
    if vol_spike_v >= 4.0:
        return True
    if abs(vol_z) >= 3.0:
        return True

    # NEW: Liquidation cascade
    cascade_risk = float(v.get("cascade_risk") or 0.0)
    if cascade_risk >= 0.6:
        return True

    # NEW: Liquidity withdrawal
    liq_withdrawal = float(v.get("liquidity_withdrawal") or 0.0)
    if liq_withdrawal >= 0.7:
        return True

    # NEW: Funding extreme
    funding_extreme = float(v.get("funding_extreme_flag") or 0.0)
    if funding_extreme >= 1.0:
        return True

    return False
```

### E2. Использовать cascade_risk и spread_dynamics в logits

**Файл:** `market_intelligence/regime.py`, метод `_classify()`

Добавить после существующих interaction terms:
```python
# Liquidation cascade amplifies panic
cascade_risk = float(v.get("cascade_risk") or 0.0)
cascade_dir = float(v.get("cascade_direction") or 0.0)
if cascade_risk > 0.3:
    logits[MarketRegime.PANIC] += 0.25 * cascade_risk * s
    logits[MarketRegime.HIGH_VOLATILITY] += 0.15 * cascade_risk * s
    if cascade_dir < 0:  # long squeeze
        logits[MarketRegime.TREND_DOWN] += 0.1 * cascade_risk * s
    elif cascade_dir > 0:  # short squeeze
        logits[MarketRegime.TREND_UP] += 0.1 * cascade_risk * s

# Spread dynamics: widening spread = incoming volatility
spread_regime = float(v.get("spread_regime_code") or 0.0)
liq_withdrawal = float(v.get("liquidity_withdrawal") or 0.0)
if spread_regime >= 2.0 or liq_withdrawal > 0.5:
    logits[MarketRegime.HIGH_VOLATILITY] += 0.2 * s
    logits[MarketRegime.RANGE] -= 0.15 * s

# Funding mean reversion signal
funding_mr = float(v.get("funding_mean_reversion_signal") or 0.0)
if funding_mr > 0.5:
    # Strong mean reversion expected → regime shift likely
    logits[MarketRegime.RANGE] += 0.15 * funding_mr * s
```

---

## БЛОК F: OUTPUT И REPORTING

### F1. Добавить новые данные в отчёт

**Файл:** `market_intelligence/output.py`, функция `format_human_report()`

Добавить после секции "УСЛОВИЯ ТОРГОВЛИ":

```python
# Microstructure section (only if data available)
btc_features = payload.get("features", {}).get(btc_sym, {})
cascade_risk = btc_features.get("cascade_risk")
liq_withdrawal = btc_features.get("liquidity_withdrawal")
funding_mr = btc_features.get("funding_mean_reversion_signal")

micro_lines = []
if cascade_risk is not None and float(cascade_risk) > 0.15:
    stage = int(float(btc_features.get("cascade_stage") or 0))
    stage_names = {0: "нет", 1: "ранний", 2: "развивающийся", 3: "АКТИВНЫЙ"}
    micro_lines.append(f"Каскад ликвидаций: {stage_names.get(stage, '?')} (риск {float(cascade_risk):.0%})")

if liq_withdrawal is not None and float(liq_withdrawal) > 0.3:
    micro_lines.append(f"Отток ликвидности: {float(liq_withdrawal):.0%}")

if funding_mr is not None and float(funding_mr) > 0.3:
    micro_lines.append(f"Сигнал mean-reversion фандинга: {float(funding_mr):.0%}")

if micro_lines:
    lines.append("МИКРОСТРУКТУРА")
    lines.extend(micro_lines)
    lines.append("")
```

### F2. Добавить контекстные заметки для новых сигналов

**Файл:** `market_intelligence/output.py`, функция `_contextual_notes()`

```python
# Cascade risk warning
cascade_risk = (payload.get("features", {}).get(btc_sym, {}) or {}).get("cascade_risk")
if cascade_risk is not None and float(cascade_risk) > 0.5:
    notes.append("Высокий риск каскадных ликвидаций — сократить позиции и подготовить хедж.")

# Liquidity withdrawal
liq_withdrawal = (payload.get("features", {}).get(btc_sym, {}) or {}).get("liquidity_withdrawal")
if liq_withdrawal is not None and float(liq_withdrawal) > 0.5:
    notes.append("Резкий отток ликвидности — возможен импульсный move на тонком рынке.")

# Funding mean reversion
funding_mr = (payload.get("features", {}).get(btc_sym, {}) or {}).get("funding_mean_reversion_signal")
if funding_mr is not None and float(funding_mr) > 0.6:
    notes.append("Фандинг в экстремальной зоне — высокая вероятность mean-reversion и разворота позиционирования.")
```

---

## БЛОК G: ИНФРАСТРУКТУРА И КАЧЕСТВО КОДА

### G1. Убрать глобальный синглтон

**Файл:** `market_intelligence/service.py`, строки 197-204

**Проблема:** Глобальный `_service` затрудняет тестирование.

**Исправление:** Оставить синглтон для backward compatibility, но добавить factory с DI:

```python
def get_market_intelligence_service(
    existing: Optional[MarketIntelligenceService] = None,
) -> MarketIntelligenceService:
    """Get or create the MI service singleton.

    Pass `existing` to override the singleton (useful for testing).
    """
    global _service
    if existing is not None:
        _service = existing
        return _service
    if _service is None:
        _service = MarketIntelligenceService()
    return _service


def reset_market_intelligence_service() -> None:
    """Reset singleton. For testing only."""
    global _service
    _service = None
```

### G2. Удалить дублирование _std_window

**Файл:** `market_intelligence/feature_engine.py`

**Проблема:** `_std_window()` дублирует `statistics.pstdev` и `RollingStats.std()`.

**Исправление:** Заменить все вызовы `_std_window(values, window)` на:
```python
from statistics import pstdev

def _std_window(values: List[float], window: int) -> float:
    if len(values) < 2:
        return 0.0
    w = values[-window:]
    if len(w) < 2:
        return 0.0
    return pstdev(w)
```

Или ещё лучше — использовать `pstdev` напрямую:
```python
from statistics import pstdev
rolling_vol = pstdev(returns[-50:]) if len(returns) >= 2 else 0.0
```

### G3. Параллелизация OHLCV fetch

**Файл:** `market_intelligence/collector.py`, метод `collect_candles()`

**Проблема:** Candles fetched sequential per symbol per timeframe. Медленно.

**Исправление:**
```python
async def collect_candles(
    self, symbols: List[str], timeframes: List[str] = ("1H",), limit: int = 100
) -> Dict[str, Dict[str, List[OHLCV]]]:
    await self.initialize()
    limiters = get_exchange_rate_limiters()
    result: Dict[str, Dict[str, List[OHLCV]]] = {s: {} for s in symbols}

    async def _fetch_one(symbol: str, tf: str) -> Tuple[str, str, List[OHLCV]]:
        for ex in self.exchanges:
            await limiters.get(ex).acquire()
            try:
                raw = await self.market_data.fetch_ohlcv(ex, symbol, tf, limit)
                if raw:
                    candles = [
                        OHLCV(
                            timestamp=int(c["ts"]),
                            open=float(c["o"]),
                            high=float(c["h"]),
                            low=float(c["l"]),
                            close=float(c["c"]),
                            volume=float(c["vol"]),
                        )
                        for c in raw
                    ]
                    candles.sort(key=lambda c: c.timestamp)
                    return symbol, tf, candles
            except Exception as e:
                logger.warning("OHLCV fetch %s/%s/%s: %s", ex, symbol, tf, e)
        return symbol, tf, []

    tasks = [_fetch_one(s, tf) for s in symbols for tf in timeframes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            logger.warning("OHLCV gather exception: %s", r)
            continue
        symbol, tf, candles = r
        if candles:
            result[symbol][tf] = candles
            self.state.ohlcv[symbol][tf] = candles

    return result
```

### G4. Health check расширение

**Файл:** `market_intelligence/service.py`, метод `health_check()`

Добавить информацию о новых компонентах:
```python
async def health_check(self) -> Dict[str, Any]:
    result = {
        "initialized": self._engine is not None,
        "last_report_age_seconds": None,
        "last_report_status": None,
        "exchanges": [],
        "symbols_count": 0,
        "order_flow_enabled": False,
        "ml_weights_active": False,
        "dead_code_integrated": True,  # Flag that all modules are connected
    }
    if self._cfg:
        result["exchanges"] = self._cfg.exchanges
        result["symbols_count"] = len(self._cfg.symbols)
        result["order_flow_enabled"] = self._cfg.order_flow_enabled
        result["ml_weights_active"] = self._cfg.adaptive_ml_weighting
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

---

## БЛОК H: ТЕСТЫ

### H1. Обновить существующие тесты

После всех изменений запустить `python -m pytest tests/ -v`. Для каждого сломанного теста:
1. Прочитать тест
2. Понять что он проверяет
3. Если тест проверяет старое поведение которое мы корректно исправили — обновить ожидания
4. Если тест выявил баг в наших изменениях — исправить код

### H2. Добавить новые тесты

**Файл:** `tests/test_market_intelligence.py` (или новый `tests/test_mi_integration.py`)

Добавить тесты для:

1. **Volume-weighted aggregation** — проверить что per-exchange volumes различаются
2. **Score нормализация** — проверить что score стабилен при N=1, N=3, N=10
3. **Cascade risk detection** — проверить что cascade_stage=3 добавляет alert
4. **Funding mean reversion** — проверить что extreme funding создаёт сигнал
5. **Portfolio VaR** — проверить что allocation уменьшается при high VaR
6. **_extract_base_currency** — проверить "1000PEPEUSDT" → "PEPE", "ETHUSDT" → "ETH"
7. **Single-symbol scoring** — проверить что score при 1 символе ∈ [0, 100]

```python
def test_extract_base_currency():
    from market_intelligence.portfolio import PortfolioAnalyzer
    assert PortfolioAnalyzer._extract_base_currency("ETHUSDT") == "ETH"
    assert PortfolioAnalyzer._extract_base_currency("BTCUSDC") == "BTC"
    assert PortfolioAnalyzer._extract_base_currency("1000PEPEUSDT") == "PEPE"
    assert PortfolioAnalyzer._extract_base_currency("1000SHIBUSDT") == "SHIB"
    assert PortfolioAnalyzer._extract_base_currency("SOLUSDT") == "SOL"


def test_single_symbol_score_stability():
    """Score for single symbol should be in [0, 100] range."""
    from market_intelligence.scorer import OpportunityScorer
    from market_intelligence.models import FeatureVector, RegimeState, MarketRegime

    scorer = OpportunityScorer()
    fv = FeatureVector(
        symbol="BTCUSDT",
        timestamp=0,
        values={
            "rolling_volatility_local": 0.5,
            "bb_width_local": 0.3,
            "funding_rate": 0.001,
            "funding_delta": 0.0001,
            "oi_delta": 100,
            "oi_delta_pct": 2.0,
            "funding_pct": 0.1,
            "spread_bps": 5.0,
            "volume_proxy": 1000,
            "orderbook_imbalance": 0.1,
            "basis_bps": 5.0,
            "basis_acceleration": 0.1,
            "funding_slope": 0.1,
            "data_quality_code": 0.0,
            "cvd": 0.3,
            "rolling_volatility": 0.2,
        },
        normalized={
            "rolling_volatility_local": 0.5,
            "bb_width_local": 0.3,
            "funding_rate": 0.4,
            "funding_delta": 0.2,
            "oi_delta": 0.3,
            "rolling_volatility": 0.2,
        },
    )
    regime = RegimeState(
        regime=MarketRegime.TREND_UP,
        confidence=0.7,
        probabilities={MarketRegime.TREND_UP: 0.7},
        stable_for_cycles=5,
    )

    result = scorer.score(
        features={"BTCUSDT": fv},
        local_regimes={"BTCUSDT": regime},
        correlations_to_btc={"BTCUSDT": 1.0},
        spread_correlations_to_btc={"BTCUSDT": 1.0},
    )

    assert len(result) == 1
    assert 0.0 <= result[0].score <= 100.0
```

---

## ФИНАЛЬНАЯ ПРОВЕРКА

После завершения всех блоков:

1. `python -m pytest tests/ -v` — все тесты зелёные
2. `python -c "from market_intelligence.service import get_market_intelligence_service; print('OK')"` — импорт работает
3. Проверить что НЕТ мёртвого кода:
   - `order_flow.py` — интегрирован через config flag
   - `funding_zscore_adaptive` — вызывается в feature_engine
   - `liquidation_cascade_risk` — вызывается в feature_engine
   - `spread_dynamics` — вызывается в feature_engine
   - `estimate_market_impact` — вызывается в feature_engine
4. Проверить что volume-weighted aggregation использует per-exchange данные
5. Проверить что OHLCV candles используются как primary source для ATR/ADX/BB
6. Проверить что scoring стабилен при N=1

Система должна отвечать критериям:
- **Трейдер:** Все индикаторы корректны, regime model реагирует на экстремальные ситуации быстро, portfolio risk учитывает cascade risk и mean-reversion, нет искажённых сигналов
- **IT-специалист:** Нет мёртвого кода, нет data pipeline bugs, стабильная нормализация, тесты покрывают edge cases, чистая архитектура
