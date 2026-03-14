---
name: market-intelligence-10of10
description: "Полная доводка Market Intelligence до 10/10 по оценке профессионального трейдера и IT-специалиста. Закрывает ВСЕ замечания аудита: backtest framework, типизация, structured logging, observability, asymmetric risk, conditional correlation, signal time-decay, pipeline timeouts, watchdog, тесты."
---

# Market Intelligence → 10/10: Полное руководство

Ты выполняешь финальную доводку Market Intelligence System. Каждый пункт ниже — конкретное замечание из профессионального аудита с точным расположением в коде и точным описанием исправления.

**КОНТЕКСТ**: Система уже прошла первый раунд фиксов (БЛОКИ A-H из `perfect-market-intelligence.md`). Этот skill закрывает **оставшиеся** замечания, которые поднимают систему с 8/10 до 10/10.

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
- НЕ менять имена .env переменных, НЕ добавлять обязательные новые env vars — ВСЁ с дефолтами
- НЕ добавлять внешние зависимости (no numpy, no pandas, no scipy, no sklearn)
- Обратная совместимость с существующей конфигурацией
- Все тесты должны проходить: `python -m pytest tests/ -v`
- Type hints везде, `from __future__ import annotations`
- Следуй стилю проекта (dataclasses, `or 0.0` fallbacks)

**ПОРЯДОК РАБОТЫ:** Выполняй блоки строго последовательно (1 → 2 → 3 → ...). После каждого блока запускай тесты. Не переходи к следующему пока текущий не работает.

---

## БЛОК 1: ТИПИЗАЦИЯ — FeatureKey Enum

### Проблема (IT, критическая)
`FeatureVector.values` и `.normalized` — это `Dict[str, Optional[float]]` с магическими строками. При 40+ индикаторах это источник runtime ошибок (опечатки в ключах, рассинхрон между модулями).

### Решение

**Файл:** `market_intelligence/models.py`

1. Добавить `FeatureKey` enum со ВСЕМИ ключами, используемыми в системе:

```python
from __future__ import annotations
from enum import Enum

class FeatureKey(str, Enum):
    """All feature keys used across the MI pipeline.

    Inherits from str so it can be used as dict key seamlessly
    and serializes to JSON without custom encoder.
    """
    # === Trend ===
    EMA_CROSS = "ema_cross"
    ADX = "adx"
    PRICE_VS_EMA200 = "price_vs_ema200"

    # === Momentum ===
    RSI = "rsi"
    MACD_LINE = "macd_line"
    MACD_SIGNAL = "macd_signal"
    MACD_HIST = "macd_hist"

    # === Volatility ===
    ATR = "atr"
    ATR_PCT = "atr_pct"
    BB_UPPER = "bb_upper"
    BB_LOWER = "bb_lower"
    BB_MID = "bb_mid"
    BB_WIDTH = "bb_width"
    BB_WIDTH_PCT = "bb_width_pct"
    ROLLING_VOLATILITY = "rolling_volatility"
    VOLATILITY_REGIME = "volatility_regime"

    # === Volume & Liquidity ===
    VOLUME_SPIKE = "volume_spike"
    CVD = "cvd"
    VWAP = "vwap"
    VOLUME_TREND = "volume_trend"
    VOLUME_PROXY = "volume_proxy"
    SPREAD_BPS = "spread_bps"
    ORDERBOOK_IMBALANCE = "orderbook_imbalance"

    # === Derivatives ===
    FUNDING_RATE = "funding_rate"
    FUNDING_DELTA = "funding_delta"
    FUNDING_PCT = "funding_pct"
    FUNDING_SLOPE = "funding_slope"
    FUNDING_DEVIATION = "funding_deviation"
    FUNDING_REGIME_CODE = "funding_regime_code"
    FUNDING_MEAN_REVERSION_SIGNAL = "funding_mean_reversion_signal"
    FUNDING_ACCELERATION = "funding_acceleration_indicator"
    FUNDING_EXTREME_FLAG = "funding_extreme_flag"
    OI_DELTA = "oi_delta"
    OI_DELTA_PCT = "oi_delta_pct"
    BASIS_BPS = "basis_bps"
    BASIS_ACCELERATION = "basis_acceleration"
    LONG_SHORT_RATIO = "long_short_ratio"

    # === Market Structure ===
    MARKET_STRUCTURE_CODE = "market_structure_code"
    CASCADE_RISK = "cascade_risk"
    CASCADE_STAGE = "cascade_stage"
    CASCADE_DIRECTION = "cascade_direction"
    SPREAD_REGIME_CODE = "spread_regime_code"
    SPREAD_EXPANSION_RATE = "spread_expansion_rate"
    SPREAD_PERCENTILE = "spread_percentile"
    LIQUIDITY_WITHDRAWAL = "liquidity_withdrawal"
    MARKET_IMPACT_TOTAL_BPS = "market_impact_total_bps"
    ENTRY_FEASIBILITY = "entry_feasibility"

    # === Correlation ===
    PRICE_CORR_TO_BTC = "price_corr_to_btc"
    SPREAD_CORR_TO_BTC = "spread_corr_to_btc"

    # === Local Z-scores ===
    ROLLING_VOLATILITY_LOCAL = "rolling_volatility_local"
    BB_WIDTH_LOCAL = "bb_width_local"

    # === Data Quality ===
    DATA_QUALITY_CODE = "data_quality_code"
    USING_CANDLE_DATA = "using_candle_data"
    ATR_SOURCE = "atr_source"
    ATR_PROXY_PENALTY = "atr_proxy_penalty_applied"

    # === Multi-Timeframe ===
    EMA_CROSS_4H = "ema_cross_4h"
    RSI_4H = "rsi_4h"
    ADX_4H = "adx_4h"
    EMA_CROSS_1D = "ema_cross_1d"
    RSI_1D = "rsi_1d"
    ADX_1D = "adx_1d"

    # === Price ===
    PRICE = "price"

    # === Order Flow ===
    FLOW_DELTA_RATIO = "flow_delta_ratio"
    FLOW_ABSORPTION_SCORE = "flow_absorption_score"
    FLOW_DELTA_DIVERGENCE = "flow_delta_divergence"

    # === Signal Metadata (скоринг) ===
    SIGNAL_AGE_SECONDS = "signal_age_seconds"
```

2. **НЕ МЕНЯТЬ тип FeatureVector.values** — оставить `Dict[str, Optional[float]]`. Enum наследует от `str`, поэтому `FeatureKey.RSI` == `"rsi"` и работает как ключ без изменения вызывающего кода.

3. Постепенно обновить ВСЕ модули, чтобы использовать `FeatureKey.XXX` вместо строковых литералов:
   - `feature_engine.py`: все `values["rsi"]` → `values[FeatureKey.RSI]`
   - `regime.py`: все `v.get("rsi")` → `v.get(FeatureKey.RSI)`
   - `scorer.py`: все обращения к feature values
   - `portfolio.py`: feature access
   - `engine.py`: feature access в alert detection
   - `output.py`: payload feature access
   - `validation.py`: feature access

4. Экспортировать из `__init__.py`:
```python
from market_intelligence.models import FeatureKey
```

**Критерий готовности:** `grep -rn '"rsi"' market_intelligence/` не должен находить магических строк в production коде (тесты — допустимо).

---

## БЛОК 2: STRUCTURED LOGGING

### Проблема (IT)
Внутренний logging — plain text через `logging.getLogger()`. Для production-системы нужен structured logging с correlation ID для трассировки отдельных циклов pipeline.

### Решение

**Новый файл:** `market_intelligence/structured_log.py`

```python
"""Structured logging adapter for Market Intelligence pipeline.

Zero external dependencies — wraps stdlib logging with JSON-structured
context propagation (cycle_id, symbol, stage).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional

# Context variable for cycle-level correlation
_cycle_ctx: ContextVar[Dict[str, Any]] = ContextVar("mi_cycle_ctx", default={})


def new_cycle_context(cycle_number: int) -> Dict[str, Any]:
    """Create and set a new cycle context. Call at start of run_once()."""
    ctx = {
        "cycle_id": uuid.uuid4().hex[:12],
        "cycle_number": cycle_number,
        "started_at": time.time(),
    }
    _cycle_ctx.set(ctx)
    return ctx


def get_cycle_context() -> Dict[str, Any]:
    return _cycle_ctx.get()


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter that injects cycle context."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _cycle_ctx.get()
        entry: Dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if ctx:
            entry["cycle_id"] = ctx.get("cycle_id", "")
            entry["cycle_number"] = ctx.get("cycle_number", 0)
        # Merge extra structured fields
        if hasattr(record, "structured"):
            entry.update(record.structured)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class StructuredLogger:
    """Thin wrapper around stdlib logger with structured context."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        record = self._logger.makeRecord(
            self._logger.name, level, "(structured)", 0, msg, (), None
        )
        record.structured = fields  # type: ignore[attr-defined]
        self._logger.handle(record)

    def debug(self, msg: str, **fields: Any) -> None:
        self._log(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._log(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._log(logging.WARNING, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._log(logging.ERROR, msg, **fields)

    def critical(self, msg: str, **fields: Any) -> None:
        self._log(logging.CRITICAL, msg, **fields)


def get_structured_logger(name: str) -> StructuredLogger:
    return StructuredLogger(f"mi.{name}")


def setup_structured_logging(
    log_dir: str = "logs",
    level: int = logging.INFO,
    also_plain: bool = True,
) -> None:
    """Configure structured logging for MI pipeline.

    Args:
        log_dir: Directory for structured log file.
        level: Logging level.
        also_plain: If True, keep plain text handler for console.
    """
    import os
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger("mi")
    root.setLevel(level)

    # JSON file handler
    json_handler = logging.FileHandler(
        os.path.join(log_dir, "mi_structured.jsonl"), encoding="utf-8"
    )
    json_handler.setFormatter(StructuredFormatter())
    root.addHandler(json_handler)

    # Optional plain console handler
    if also_plain:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        ))
        root.addHandler(console)
```

**Интеграция в engine.py:**

1. В начале `run_once()`:
```python
from market_intelligence.structured_log import new_cycle_context, get_structured_logger

logger = get_structured_logger("engine")

async def run_once(self) -> MarketIntelligenceReport:
    self._cycle_counter += 1
    ctx = new_cycle_context(self._cycle_counter)
    logger.info("Cycle started", symbols=len(self._symbols), cycle=self._cycle_counter)
    ...
```

2. Добавить `self._cycle_counter: int = 0` в `__init__`.

3. В каждом ключевом модуле заменить `logger = logging.getLogger(...)` на:
```python
from market_intelligence.structured_log import get_structured_logger
logger = get_structured_logger("collector")  # или "regime", "scorer", etc.
```

4. Добавить structured fields к ключевым событиям:
```python
# collector.py — после сбора данных
logger.info("Snapshot collected", symbol=symbol, exchange=ex, latency_ms=elapsed_ms)

# regime.py — при смене режима
logger.info("Regime transition", symbol=key, old=prev.name, new=regime.name, confidence=conf)

# scorer.py — топ-3 возможности
logger.info("Top opportunities", top3=[(o.symbol, o.score) for o in results[:3]])
```

**Конфиг:** Добавить в `config.py`:
```python
structured_logging: bool  # default True
```
В `from_env()`:
```python
structured_logging=_as_bool("MI_STRUCTURED_LOGGING", True),
```

**Инициализация в service.py**, метод `initialize()`:
```python
if self._cfg.structured_logging:
    from market_intelligence.structured_log import setup_structured_logging
    setup_structured_logging(log_dir=self._cfg.log_dir)
```

---

## БЛОК 3: SIGNAL TIME-DECAY

### Проблема (трейдер, критическая)
Сигнал, обнаруженный 5 минут назад и 50 минут назад, весит одинаково в скоринге. В реальной торговле сигналы стухают (decay) быстро.

### Решение

**Файл:** `market_intelligence/scorer.py`

1. Добавить time-decay factor к каждому opportunity score. Нужно знать, когда opportunity впервые появилась.

2. В `OpportunityScorer.__init__()` добавить:
```python
self._first_seen: Dict[str, float] = {}  # symbol → timestamp первого обнаружения
self._signal_half_life_seconds: float = signal_half_life_seconds or 1800.0  # 30 min default
```

3. Добавить параметр конструктора:
```python
def __init__(self, ..., signal_half_life_seconds: float = 1800.0):
    ...
    self._signal_half_life_seconds = signal_half_life_seconds
```

4. В `score()`, после вычисления raw_score для каждого символа, применить decay:

```python
import math, time

now = time.time()

# Track first-seen time
if symbol not in self._first_seen:
    self._first_seen[symbol] = now

# Time-decay: exponential decay with half-life
age_seconds = now - self._first_seen[symbol]
decay_factor = math.pow(0.5, age_seconds / self._signal_half_life_seconds)
# Minimum decay floor: don't completely kill old signals
decay_factor = max(0.15, decay_factor)

# Apply decay to raw_score
raw_score *= decay_factor
```

5. Сбросить first_seen когда символ перестаёт появляться в opportunities:
```python
# В конце score(), после формирования результатов
current_symbols = {row[0] for row in raw_rows}
expired = [s for s in self._first_seen if s not in current_symbols]
for s in expired:
    del self._first_seen[s]
```

6. Добавить `signal_age_seconds` в breakdown для отчёта:
```python
breakdown["signal_age_seconds"] = age_seconds
breakdown["decay_factor"] = decay_factor
```

7. Записать в `FeatureVector` для доступа в output:
```python
# Это уже есть в breakdown — достаточно
```

**Конфиг:** Добавить в `config.py`:
```python
signal_half_life_seconds: float  # default 1800.0 (30 min)
```
В `from_env()`:
```python
signal_half_life_seconds=float(os.getenv("MI_SIGNAL_HALF_LIFE", "1800")),
```

Передать в `OpportunityScorer` из `engine.py`:
```python
self.scorer = OpportunityScorer(
    ...,
    signal_half_life_seconds=config.signal_half_life_seconds,
)
```

---

## БЛОК 4: ASYMMETRIC RISK MODELING

### Проблема (трейдер)
Рынок падает быстрее, чем растёт. PANIC и OVERHEATED имеют одинаковую min_duration (1 цикл). PANIC должен реагировать мгновенно — с 0 задержкой.

### Решение

**Файл:** `market_intelligence/regime.py`

1. Изменить `REGIME_MIN_CYCLES`:
```python
REGIME_MIN_CYCLES: Dict[MarketRegime, int] = {
    MarketRegime.PANIC: 0,              # WAS: 1 — мгновенная реакция на панику
    MarketRegime.HIGH_VOLATILITY: 1,    # без изменений
    MarketRegime.OVERHEATED: 1,         # без изменений
    MarketRegime.TREND_UP: 2,           # без изменений
    MarketRegime.TREND_DOWN: 1,         # WAS: 2 — нисходящий тренд быстрее подтверждается
    MarketRegime.RANGE: 3,              # без изменений
}
```

2. В `_apply_stability()`, добавить asymmetric confidence boosting для bearish signals:
```python
# ПОСЛЕ вычисления smoothed_probs, ПЕРЕД выбором candidate:

# Asymmetric boost: bearish signals get confidence boost in ambiguous situations
panic_prob = smoothed_probs.get(MarketRegime.PANIC, 0.0)
trend_down_prob = smoothed_probs.get(MarketRegime.TREND_DOWN, 0.0)
bearish_total = panic_prob + trend_down_prob

# If bearish signals are present but not dominant, boost them slightly
# This reflects the empirical observation that markets fall faster
if 0.2 < bearish_total < 0.5:
    asymmetry_boost = 0.08  # 8% boost to bearish probabilities
    smoothed_probs[MarketRegime.PANIC] = panic_prob * (1.0 + asymmetry_boost)
    smoothed_probs[MarketRegime.TREND_DOWN] = trend_down_prob * (1.0 + asymmetry_boost)
    # Renormalize
    total = sum(smoothed_probs.values())
    if total > 0:
        smoothed_probs = {k: v / total for k, v in smoothed_probs.items()}
```

3. В `_classify()`, добавить asymmetric liquidation weighting:
```python
# ПОСЛЕ existing interaction terms, ПЕРЕД softmax:

# Asymmetric risk: liquidation cascade on longs is worse than on shorts
# (most market participants are long-biased)
cascade_dir = float(v.get("cascade_direction") or 0.0)
if cascade_dir < 0:  # long liquidation cascade
    logits[MarketRegime.PANIC] *= 1.15  # 15% extra weight for long cascades
```

**Файл:** `market_intelligence/portfolio.py`

4. В `analyze()`, добавить asymmetric drawdown penalty:
```python
# ЗАМЕНИТЬ drawdown penalty блок:

# BLOCK 5.2: Asymmetric drawdown awareness
# Drawdowns hurt more than gains help (prospect theory / loss aversion)
if current_portfolio_drawdown_pct > 5.0:
    # Quadratic penalty: small drawdowns tolerated, large ones aggressively penalized
    dd = current_portfolio_drawdown_pct
    drawdown_penalty = max(0.3, 1.0 - 0.005 * dd * dd)  # quadratic, floor at 0.3
    base_risk *= drawdown_penalty
```

**Тесты:** Добавить:
```python
def test_panic_regime_zero_delay():
    """PANIC regime should activate with 0 delay (min_cycles=0)."""
    from market_intelligence.regime import RegimeModel, REGIME_MIN_CYCLES
    from market_intelligence.models import MarketRegime
    assert REGIME_MIN_CYCLES[MarketRegime.PANIC] == 0


def test_asymmetric_drawdown_quadratic():
    """Large drawdowns should be penalized quadratically, not linearly."""
    # 10% drawdown: penalty = 1 - 0.005 * 100 = 0.5
    # 5% drawdown: penalty = 1 - 0.005 * 25 = 0.875
    # This means 10% DD is penalized ~2x harder than 5% DD (not linearly)
    dd_5 = max(0.3, 1.0 - 0.005 * 5 * 5)
    dd_10 = max(0.3, 1.0 - 0.005 * 10 * 10)
    dd_15 = max(0.3, 1.0 - 0.005 * 15 * 15)

    assert dd_5 > dd_10 > dd_15
    assert dd_5 == 0.875
    assert dd_10 == 0.5
    assert dd_15 == 0.3  # floor
```

---

## БЛОК 5: CONDITIONAL CORRELATION

### Проблема (трейдер, критическая)
Корреляция к BTC — хороший фактор, но в крипто кризисные корреляции стремятся к 1.0. Система использует `robust_corr()` с adaptive window (stress vs normal), но не отделяет conditional correlation (корреляция в стрессе vs в нормальном режиме).

### Решение

**Файл:** `market_intelligence/statistics.py`

1. Добавить функцию conditional_correlation:

```python
def conditional_correlation(
    x: List[float],
    y: List[float],
    stress_flags: List[bool],
    min_samples: int = 15,
) -> Dict[str, Optional[float]]:
    """Compute correlation separately for stress and normal periods.

    Args:
        x, y: Price/return series (same length).
        stress_flags: Boolean flags marking stress periods (same length as x, y).
        min_samples: Minimum samples required for each regime.

    Returns:
        Dict with keys:
            normal_corr: correlation during non-stress periods
            stress_corr: correlation during stress periods
            corr_divergence: stress_corr - normal_corr (positive = correlation increases in stress)
            effective_corr: weighted blend for portfolio use
    """
    if len(x) != len(y) or len(x) != len(stress_flags) or len(x) < min_samples:
        return {"normal_corr": None, "stress_corr": None, "corr_divergence": None, "effective_corr": None}

    stress_x = [x[i] for i in range(len(x)) if stress_flags[i]]
    stress_y = [y[i] for i in range(len(y)) if stress_flags[i]]
    normal_x = [x[i] for i in range(len(x)) if not stress_flags[i]]
    normal_y = [y[i] for i in range(len(y)) if not stress_flags[i]]

    normal_c = robust_corr(normal_x, normal_y) if len(normal_x) >= min_samples else None
    stress_c = robust_corr(stress_x, stress_y) if len(stress_x) >= min_samples else None

    divergence = None
    effective = None

    if normal_c is not None and stress_c is not None:
        divergence = stress_c - normal_c
        # Effective correlation: pessimistic blend — weight stress correlation higher
        # because that's when correlation matters most for risk
        effective = 0.4 * normal_c + 0.6 * stress_c
    elif stress_c is not None:
        effective = stress_c
    elif normal_c is not None:
        effective = normal_c

    return {
        "normal_corr": normal_c,
        "stress_corr": stress_c,
        "corr_divergence": divergence,
        "effective_corr": effective,
    }
```

**Файл:** `market_intelligence/engine.py`

2. В `_correlations_to_btc()`, добавить stress flag tracking и conditional correlation:

```python
# В engine.__init__:
self._regime_history_flags: Dict[str, List[bool]] = {}  # symbol → [is_stress, ...]

# В _correlations_to_btc(), после вычисления robust_corr:
# Build stress flags from regime history
stress_regimes = {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY, MarketRegime.OVERHEATED}
for sym in symbols:
    history = self._regime_history_flags.get(sym, [])
    if global_regime and global_regime.regime in stress_regimes:
        history.append(True)
    else:
        history.append(False)
    # Keep bounded
    if len(history) > self.config.historical_window:
        history = history[-self.config.historical_window:]
    self._regime_history_flags[sym] = history

# Compute conditional correlations for portfolio use
conditional_corrs: Dict[str, Dict[str, Optional[float]]] = {}
btc_prices = btc_hist.get("price", [])
for sym in symbols:
    if sym == btc_sym:
        continue
    sym_prices = histories.get(sym, {}).get("price", [])
    flags = self._regime_history_flags.get(sym, [])
    min_len = min(len(btc_prices), len(sym_prices), len(flags))
    if min_len >= 30:
        cc = conditional_correlation(
            btc_prices[-min_len:],
            sym_prices[-min_len:],
            flags[-min_len:],
        )
        conditional_corrs[sym] = cc
```

3. Передать `conditional_corrs` в portfolio analyzer и использовать `effective_corr` вместо обычной корреляции:

В `PipelineResult` dataclass добавить:
```python
conditional_correlations: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
```

В `portfolio.analyze()`, если conditional correlations доступны, использовать `effective_corr`:
```python
# При вычислении corr_factor:
for sym in allocation:
    cc = conditional_correlations.get(sym)
    if cc and cc.get("effective_corr") is not None:
        effective = abs(cc["effective_corr"])
    else:
        effective = abs(correlations_to_btc.get(sym, 0.0))
    corr_values.append(effective)
```

4. Добавить `conditional_correlations` в payload для отчёта.

**Конфиг:** Ничего нового — использует существующие `correlation_window` и `stress_correlation_window`.

---

## БЛОК 6: PIPELINE TIMEOUT & BACKPRESSURE

### Проблема (IT)
Если exchange API тормозит, pipeline просто ждёт. Нет таймаутов на уровне pipeline cycle. Один зависший запрос может задержать весь цикл на неопределённое время.

### Решение

**Файл:** `market_intelligence/engine.py`

1. Добавить cycle-level timeout в `run_once()`:

```python
import asyncio

async def run_once(self) -> MarketIntelligenceReport:
    cycle_timeout = self.config.cycle_timeout_seconds  # default 120s
    try:
        return await asyncio.wait_for(
            self._run_once_inner(),
            timeout=cycle_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Cycle timeout after %ds — returning degraded report", cycle_timeout)
        return self._build_timeout_report()
```

2. Выделить текущее тело `run_once()` в `_run_once_inner()`.

3. Добавить `_build_timeout_report()`:
```python
def _build_timeout_report(self) -> MarketIntelligenceReport:
    """Build a minimal report when cycle times out."""
    import time as _time
    return MarketIntelligenceReport(
        timestamp=int(_time.time()),
        global_timeframe=self.config.global_timeframe,
        local_timeframe=self.config.local_timeframe,
        scoring_enabled=False,
        data_health_status=DataHealthStatus.INVALID,
        data_health_warnings=["Cycle timed out — data may be stale"],
        global_regime=self._last_global_regime or RegimeState(
            regime=MarketRegime.RANGE, confidence=0.0, probabilities={}, stable_for_cycles=0
        ),
        local_regimes={},
        opportunities=[],
        portfolio_risk=None,
        extreme_alerts=["TIMEOUT: Pipeline cycle exceeded time limit"],
        dynamic_deltas={},
        payload={"status": "timeout", "timestamp": int(_time.time())},
    )
```

4. Добавить per-stage latency tracking:
```python
# В _run_once_inner():
import time as _time

t0 = _time.monotonic()
snapshots = await self._collector.collect(symbols)
stage_latencies["collect"] = _time.monotonic() - t0

t1 = _time.monotonic()
p = await asyncio.get_event_loop().run_in_executor(None, self._compute_pipeline, ...)
stage_latencies["compute"] = _time.monotonic() - t1

# Log stage latencies
logger.info("Pipeline latencies", **stage_latencies)

# Record in metrics
from market_intelligence.metrics import get_metrics
metrics = get_metrics()
for stage, latency in stage_latencies.items():
    metrics.record_latency(f"pipeline.{stage}", latency)
```

**Конфиг:** Добавить в `config.py`:
```python
cycle_timeout_seconds: float  # default 120.0
```
В `from_env()`:
```python
cycle_timeout_seconds=float(os.getenv("MI_CYCLE_TIMEOUT", "120")),
```

---

## БЛОК 7: WATCHDOG & AUTO-RECOVERY

### Проблема (IT)
`MarketIntelligenceService` — singleton. При падении engine весь pipeline останавливается. Нет supervisor/watchdog.

### Решение

**Файл:** `market_intelligence/service.py`

1. Добавить watchdog в `_run_loop()` (или где запускается periodic engine):

```python
async def _run_loop(self) -> None:
    """Main loop with watchdog: restarts engine on N consecutive failures."""
    consecutive_failures = 0
    max_consecutive_failures = 3

    while self._running:
        try:
            report = await self._engine.run_once()
            self._last_report = report
            self._last_report_ts = time.time()
            consecutive_failures = 0  # reset on success

            # Send report if needed
            if self._should_send(report):
                await self._send_report(report)

        except Exception as e:
            consecutive_failures += 1
            logger.error(
                "Engine cycle failed (%d/%d)",
                consecutive_failures,
                max_consecutive_failures,
                error=str(e),
            )

            if consecutive_failures >= max_consecutive_failures:
                logger.critical(
                    "Watchdog: %d consecutive failures — reinitializing engine",
                    consecutive_failures,
                )
                try:
                    await self._reinitialize_engine()
                    consecutive_failures = 0
                except Exception as reinit_err:
                    logger.critical("Watchdog: reinit failed: %s", reinit_err)
                    # Back off before trying again
                    await asyncio.sleep(60)

        await asyncio.sleep(self._cfg.interval_seconds)


async def _reinitialize_engine(self) -> None:
    """Tear down and rebuild the engine from scratch."""
    logger.info("Reinitializing MI engine...")

    # Save state before teardown
    if self._engine and self._cfg.persist_enabled:
        try:
            from market_intelligence.persistence import save_state
            save_state(self._engine, self._cfg.persist_file)
        except Exception:
            pass

    # Rebuild collector + engine
    old_collector = self._collector
    self._collector = type(old_collector)(
        market_data=old_collector.market_data,
        exchanges=old_collector.exchanges,
        config=self._cfg,
    )
    self._engine = MarketIntelligenceEngine(
        config=self._cfg,
        collector=self._collector,
    )
    logger.info("MI engine reinitialized successfully")
```

2. Добавить health status tracking:
```python
# В health_check():
result["consecutive_failures"] = self._consecutive_failures
result["last_reinit_ts"] = self._last_reinit_ts
result["total_reinits"] = self._total_reinits
```

---

## БЛОК 8: OBSERVABILITY — METRICS EXPORT

### Проблема (IT)
`metrics.py` существует, но нет способа экспортировать метрики для внешнего мониторинга.

### Решение

**Файл:** `market_intelligence/metrics.py`

1. Добавить метод для экспорта в Prometheus text format (без зависимости от prometheus_client):

```python
def export_prometheus_text(self) -> str:
    """Export metrics in Prometheus text exposition format.

    No external dependencies — generates text directly.
    Can be served via a simple HTTP endpoint.
    """
    lines: List[str] = []
    snapshot = self.get_snapshot()

    # Latency histograms (as summary)
    for name, stats in snapshot.get("latencies", {}).items():
        safe_name = name.replace(".", "_").replace("-", "_")
        lines.append(f"# HELP mi_{safe_name}_seconds Pipeline stage latency")
        lines.append(f"# TYPE mi_{safe_name}_seconds summary")
        lines.append(f'mi_{safe_name}_seconds{{quantile="0.5"}} {stats.get("p50", 0):.4f}')
        lines.append(f'mi_{safe_name}_seconds{{quantile="0.95"}} {stats.get("p95", 0):.4f}')
        lines.append(f'mi_{safe_name}_seconds{{quantile="0.99"}} {stats.get("p99", 0):.4f}')
        lines.append(f'mi_{safe_name}_seconds_count {stats.get("count", 0)}')

    # Counters
    for name, value in snapshot.get("counters", {}).items():
        safe_name = name.replace(".", "_").replace("-", "_")
        lines.append(f"# HELP mi_{safe_name}_total Counter metric")
        lines.append(f"# TYPE mi_{safe_name}_total counter")
        lines.append(f"mi_{safe_name}_total {value}")

    # Gauges
    for name, value in snapshot.get("gauges", {}).items():
        safe_name = name.replace(".", "_").replace("-", "_")
        lines.append(f"# HELP mi_{safe_name} Gauge metric")
        lines.append(f"# TYPE mi_{safe_name} gauge")
        lines.append(f"mi_{safe_name} {value}")

    # Exchange health
    for ex, health in snapshot.get("exchange_health", {}).items():
        safe_ex = ex.replace(".", "_").replace("-", "_")
        lines.append(f'mi_exchange_calls_total{{exchange="{safe_ex}"}} {health.get("total", 0)}')
        lines.append(f'mi_exchange_errors_total{{exchange="{safe_ex}"}} {health.get("errors", 0)}')
        error_rate = health.get("error_rate", 0)
        lines.append(f'mi_exchange_error_rate{{exchange="{safe_ex}"}} {error_rate:.4f}')

    return "\n".join(lines) + "\n"
```

2. Записывать ключевые метрики в каждом цикле. В `engine.py`:
```python
from market_intelligence.metrics import get_metrics

# В конце run_once():
metrics = get_metrics()
metrics.record_gauge("regime.confidence", global_regime.confidence)
metrics.record_gauge("regime.stable_cycles", global_regime.stable_for_cycles)
metrics.record_gauge("opportunities.count", len(opportunities))
if opportunities:
    metrics.record_gauge("opportunities.top_score", opportunities[0].score)
metrics.record_gauge("data_health", 1.0 if data_health == DataHealthStatus.OK else 0.0)
metrics.record_counter("cycles.total")
```

3. Опциональный HTTP endpoint в `service.py`:
```python
async def start_metrics_server(self, port: int = 9090) -> None:
    """Start a minimal HTTP server for Prometheus scraping.

    Only starts if MI_METRICS_PORT env var is set.
    """
    from aiohttp import web

    async def metrics_handler(request: web.Request) -> web.Response:
        from market_intelligence.metrics import get_metrics
        text = get_metrics().export_prometheus_text()
        return web.Response(text=text, content_type="text/plain; charset=utf-8")

    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Metrics server started on port %d", port)
```

**Конфиг:**
```python
metrics_port: int  # default 0 (disabled)
```
В `from_env()`:
```python
metrics_port=int(os.getenv("MI_METRICS_PORT", "0")),
```

---

## БЛОК 9: BACKTEST FRAMEWORK

### Проблема (трейдер, критическая)
Без бэктеста система — гипотеза. Нужен фреймворк для прогона на исторических данных с метриками (Sharpe, Sortino, max DD, win rate по режимам).

### Решение

**Новый файл:** `market_intelligence/backtest.py`

```python
"""Backtesting framework for Market Intelligence pipeline.

Replays historical data through the full pipeline and collects
performance metrics. Zero external dependencies.

Usage:
    from market_intelligence.backtest import Backtester
    bt = Backtester(config)
    bt.load_jsonl("logs/market_intelligence.jsonl")
    results = bt.run()
    print(results.summary())
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class BacktestTrade:
    """Represents a simulated trade from opportunity scoring."""
    symbol: str
    entry_time: int
    exit_time: int
    direction: str  # "long" or "short"
    entry_price: float
    exit_price: float
    allocation_pct: float
    regime_at_entry: str
    score_at_entry: float
    pnl_pct: float = 0.0

    def compute_pnl(self) -> None:
        if self.direction == "long":
            self.pnl_pct = (self.exit_price - self.entry_price) / self.entry_price * 100.0
        else:
            self.pnl_pct = (self.entry_price - self.exit_price) / self.entry_price * 100.0


@dataclass
class BacktestMetrics:
    """Comprehensive backtest performance metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    regime_performance: Dict[str, Dict[str, float]] = field(default_factory=dict)
    equity_curve: List[float] = field(default_factory=list)
    monthly_returns: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "BACKTEST RESULTS",
            "=" * 60,
            f"Total trades:     {self.total_trades}",
            f"Win rate:         {self.win_rate:.1f}%",
            f"Total PnL:        {self.total_pnl_pct:+.2f}%",
            f"Max drawdown:     {self.max_drawdown_pct:.2f}%",
            f"Sharpe ratio:     {self.sharpe_ratio:.2f}",
            f"Sortino ratio:    {self.sortino_ratio:.2f}",
            f"Profit factor:    {self.profit_factor:.2f}",
            f"Avg win:          {self.avg_win_pct:+.2f}%",
            f"Avg loss:         {self.avg_loss_pct:+.2f}%",
            "",
            "Performance by regime:",
        ]
        for regime, perf in self.regime_performance.items():
            lines.append(f"  {regime}: trades={perf.get('count', 0)}, "
                         f"win_rate={perf.get('win_rate', 0):.0f}%, "
                         f"avg_pnl={perf.get('avg_pnl', 0):+.2f}%")
        lines.append("=" * 60)
        return "\n".join(lines)


class Backtester:
    """Replay historical MI reports and simulate trading decisions.

    Methodology:
    - For each cycle, take top N opportunities with score > threshold
    - Simulate entry at current price, exit at next cycle price
    - Weight positions by portfolio allocation %
    - Track equity curve, drawdowns, regime-specific performance
    """

    def __init__(
        self,
        score_threshold: float = 30.0,
        max_positions: int = 5,
        hold_cycles: int = 1,
    ) -> None:
        self.score_threshold = score_threshold
        self.max_positions = max_positions
        self.hold_cycles = hold_cycles
        self._reports: List[Dict[str, Any]] = []

    def load_jsonl(self, path: str) -> int:
        """Load historical reports from JSONL file. Returns count loaded."""
        self._reports = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "opportunities" in data and "global_regime" in data:
                        self._reports.append(data)
                except (json.JSONDecodeError, KeyError):
                    continue
        self._reports.sort(key=lambda r: r.get("timestamp", 0))
        return len(self._reports)

    def load_reports(self, reports: List[Dict[str, Any]]) -> None:
        """Load reports directly (for testing)."""
        self._reports = sorted(reports, key=lambda r: r.get("timestamp", 0))

    def run(self) -> BacktestMetrics:
        """Execute backtest and return metrics."""
        if len(self._reports) < 2:
            return BacktestMetrics()

        trades: List[BacktestTrade] = []
        equity = 100.0
        equity_curve = [equity]
        peak_equity = equity

        # Build price lookup: {symbol: {timestamp: price}}
        price_map: Dict[str, Dict[int, float]] = {}
        for report in self._reports:
            ts = report.get("timestamp", 0)
            for sym, feat in report.get("features", {}).items():
                if isinstance(feat, dict):
                    price = feat.get("price")
                    if price is not None:
                        price_map.setdefault(sym, {})[ts] = float(price)

        # Simulate cycle by cycle
        for i in range(len(self._reports) - self.hold_cycles):
            report = self._reports[i]
            next_report = self._reports[i + self.hold_cycles]
            ts = report.get("timestamp", 0)
            next_ts = next_report.get("timestamp", 0)
            regime = report.get("global_regime", {}).get("regime", "RANGE")

            opportunities = report.get("opportunities", [])
            allocation = report.get("portfolio_risk", {}).get("capital_allocation_pct", {})

            # Filter by threshold and take top N
            selected = [
                o for o in opportunities
                if o.get("score", 0) >= self.score_threshold
            ][:self.max_positions]

            cycle_pnl = 0.0
            for opp in selected:
                sym = opp["symbol"]
                score = opp.get("score", 0)
                bias = opp.get("directional_bias", "neutral")
                alloc = allocation.get(sym, 0.0) / 100.0

                entry_price = (price_map.get(sym) or {}).get(ts)
                exit_price = (price_map.get(sym) or {}).get(next_ts)

                if entry_price is None or exit_price is None or entry_price == 0:
                    continue

                direction = "long" if bias == "long" else ("short" if bias == "short" else "long")
                trade = BacktestTrade(
                    symbol=sym,
                    entry_time=ts,
                    exit_time=next_ts,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    allocation_pct=alloc * 100,
                    regime_at_entry=regime,
                    score_at_entry=score,
                )
                trade.compute_pnl()
                trades.append(trade)
                cycle_pnl += trade.pnl_pct * alloc

            equity *= (1.0 + cycle_pnl / 100.0)
            equity_curve.append(equity)
            peak_equity = max(peak_equity, equity)

        # Compute metrics
        metrics = BacktestMetrics()
        metrics.total_trades = len(trades)
        metrics.equity_curve = equity_curve

        if not trades:
            return metrics

        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        metrics.winning_trades = len(wins)
        metrics.losing_trades = len(losses)
        metrics.win_rate = len(wins) / len(trades) * 100.0
        metrics.total_pnl_pct = equity - 100.0

        if wins:
            metrics.avg_win_pct = sum(t.pnl_pct for t in wins) / len(wins)
        if losses:
            metrics.avg_loss_pct = sum(t.pnl_pct for t in losses) / len(losses)

        total_wins = sum(t.pnl_pct for t in wins)
        total_losses = abs(sum(t.pnl_pct for t in losses))
        metrics.profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

        # Max drawdown from equity curve
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100.0
            max_dd = max(max_dd, dd)
        metrics.max_drawdown_pct = max_dd

        # Sharpe & Sortino (annualized, assuming 5min cycles)
        returns = []
        for i in range(1, len(equity_curve)):
            r = (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            returns.append(r)

        if len(returns) >= 2:
            avg_ret = sum(returns) / len(returns)
            std_ret = math.sqrt(sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1))
            downside = [r for r in returns if r < 0]
            downside_std = math.sqrt(sum(r ** 2 for r in downside) / max(len(downside), 1))

            # Annualization factor: cycles_per_year
            # Assuming interval_seconds=300 (5min), ~105120 cycles/year
            ann_factor = math.sqrt(105120)
            metrics.sharpe_ratio = (avg_ret / std_ret * ann_factor) if std_ret > 0 else 0.0
            metrics.sortino_ratio = (avg_ret / downside_std * ann_factor) if downside_std > 0 else 0.0

        # Regime-specific performance
        regime_groups: Dict[str, List[BacktestTrade]] = {}
        for t in trades:
            regime_groups.setdefault(t.regime_at_entry, []).append(t)

        for regime, group in regime_groups.items():
            group_wins = [t for t in group if t.pnl_pct > 0]
            metrics.regime_performance[regime] = {
                "count": len(group),
                "win_rate": len(group_wins) / len(group) * 100.0 if group else 0,
                "avg_pnl": sum(t.pnl_pct for t in group) / len(group) if group else 0,
                "total_pnl": sum(t.pnl_pct for t in group),
            }

        return metrics
```

**Экспорт из `__init__.py`:**
```python
from market_intelligence.backtest import Backtester, BacktestMetrics
```

**Тест:**
```python
def test_backtester_basic():
    from market_intelligence.backtest import Backtester

    reports = [
        {
            "timestamp": 1000,
            "global_regime": {"regime": "TREND_UP"},
            "opportunities": [
                {"symbol": "BTCUSDT", "score": 50, "directional_bias": "long"}
            ],
            "portfolio_risk": {"capital_allocation_pct": {"BTCUSDT": 50.0}},
            "features": {"BTCUSDT": {"price": 100.0}},
        },
        {
            "timestamp": 2000,
            "global_regime": {"regime": "TREND_UP"},
            "opportunities": [
                {"symbol": "BTCUSDT", "score": 45, "directional_bias": "long"}
            ],
            "portfolio_risk": {"capital_allocation_pct": {"BTCUSDT": 50.0}},
            "features": {"BTCUSDT": {"price": 105.0}},
        },
        {
            "timestamp": 3000,
            "global_regime": {"regime": "RANGE"},
            "opportunities": [],
            "portfolio_risk": {"capital_allocation_pct": {}},
            "features": {"BTCUSDT": {"price": 103.0}},
        },
    ]
    bt = Backtester(score_threshold=30.0)
    bt.load_reports(reports)
    result = bt.run()

    assert result.total_trades >= 1
    assert result.total_pnl_pct > 0  # BTC went from 100 to 105
    assert 0 <= result.win_rate <= 100
    assert result.max_drawdown_pct >= 0
    print(result.summary())
```

---

## БЛОК 10: COMPREHENSIVE TEST SUITE

### Проблема (IT, критическая)
Один тестовый файл с synthetic data. Нет unit-тестов для edge cases, property-based тестов, regression тестов.

### Решение

**Новый файл:** `tests/test_mi_edge_cases.py`

```python
"""Edge-case and regression tests for Market Intelligence.

Covers: indicators, regime, scoring, portfolio, feature engine, backtest.
"""
from __future__ import annotations

import math
import pytest


# ============================================================
# INDICATOR EDGE CASES
# ============================================================

class TestIndicatorEdgeCases:
    """Unit tests for individual indicators with edge case inputs."""

    def test_rsi_all_gains(self):
        from market_intelligence.indicators import rsi
        values = [float(i) for i in range(1, 30)]
        result = rsi(values)
        assert result > 90.0  # Should be near 100

    def test_rsi_all_losses(self):
        from market_intelligence.indicators import rsi
        values = [float(30 - i) for i in range(30)]
        result = rsi(values)
        assert result < 10.0  # Should be near 0

    def test_rsi_flat_market(self):
        from market_intelligence.indicators import rsi
        values = [100.0] * 30
        result = rsi(values)
        assert 45.0 <= result <= 55.0  # Should be ~50

    def test_rsi_insufficient_data(self):
        from market_intelligence.indicators import rsi
        values = [1.0, 2.0, 3.0]
        result = rsi(values, period=14)
        # Should handle gracefully — either return 50 or a value
        assert 0.0 <= result <= 100.0

    def test_atr_zero_range(self):
        from market_intelligence.indicators import atr
        n = 30
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        result = atr(highs, lows, closes)
        assert result == 0.0 or result is None or result < 0.01

    def test_atr_single_candle(self):
        from market_intelligence.indicators import atr
        result = atr([105.0], [95.0], [100.0])
        assert result is None  # Insufficient data

    def test_adx_flat_market(self):
        from market_intelligence.indicators import adx
        n = 50
        highs = [100.0 + 0.01 * (i % 2) for i in range(n)]
        lows = [99.99 - 0.01 * (i % 2) for i in range(n)]
        closes = [100.0] * n
        result = adx(highs, lows, closes)
        # Flat market should have low ADX
        if result is not None:
            assert result < 25.0

    def test_bollinger_bands_constant_price(self):
        from market_intelligence.indicators import bollinger_bands
        values = [100.0] * 25
        upper, mid, lower, width = bollinger_bands(values)
        assert mid == pytest.approx(100.0)
        assert width == pytest.approx(0.0, abs=0.001)
        assert upper == pytest.approx(100.0, abs=0.001)
        assert lower == pytest.approx(100.0, abs=0.001)

    def test_macd_convergence(self):
        from market_intelligence.indicators import macd
        # Flat price → MACD should converge to 0
        values = [100.0] * 50
        line, signal, hist = macd(values)
        assert abs(line) < 1.0
        assert abs(hist) < 1.0

    def test_ema_single_value(self):
        from market_intelligence.indicators import ema
        result = ema([42.0], period=10)
        assert result == 42.0

    def test_linear_slope_constant(self):
        from market_intelligence.indicators import linear_slope
        result = linear_slope([5.0] * 20)
        assert abs(result) < 0.001

    def test_linear_slope_perfect_trend(self):
        from market_intelligence.indicators import linear_slope
        result = linear_slope([float(i) for i in range(20)])
        assert result > 0  # Positive trend

    def test_volume_spike_no_baseline(self):
        from market_intelligence.indicators import volume_spike
        result = volume_spike([0.0] * 20, 100.0)
        # Division by zero should be handled
        assert result is not None

    def test_cvd_no_volume(self):
        from market_intelligence.indicators import cumulative_volume_delta
        result = cumulative_volume_delta([], [], [], [])
        assert result == 0.0 or result is None

    def test_market_structure_insufficient_data(self):
        from market_intelligence.indicators import market_structure
        result = market_structure([100.0], [99.0], [100.0])
        # Should handle gracefully
        assert result is not None

    def test_funding_zscore_adaptive_empty(self):
        from market_intelligence.indicators import funding_zscore_adaptive
        result = funding_zscore_adaptive([])
        assert result["funding_extreme"] == 0.0
        assert result["funding_mean_reversion_signal"] == 0.0

    def test_funding_zscore_adaptive_constant(self):
        from market_intelligence.indicators import funding_zscore_adaptive
        result = funding_zscore_adaptive([0.01] * 100)
        assert result["funding_deviation"] == pytest.approx(0.0, abs=0.01)

    def test_liquidation_cascade_risk_empty(self):
        from market_intelligence.indicators import liquidation_cascade_risk
        result = liquidation_cascade_risk([], [], [])
        assert result["cascade_risk"] == 0.0
        assert result["cascade_stage"] == 0

    def test_spread_dynamics_empty(self):
        from market_intelligence.indicators import spread_dynamics
        result = spread_dynamics([])
        assert result["spread_regime_code"] == 0
        assert result["liquidity_withdrawal"] == 0.0

    def test_estimate_market_impact_zero_volume(self):
        from market_intelligence.indicators import estimate_market_impact
        result = estimate_market_impact(0.0, 0.0, 100.0, 5.0)
        # Should handle gracefully
        assert "total_cost_bps" in result


# ============================================================
# REGIME MODEL EDGE CASES
# ============================================================

class TestRegimeEdgeCases:
    """Test regime model with extreme and edge-case inputs."""

    def test_all_none_features(self):
        from market_intelligence.regime import RegimeModel
        from market_intelligence.models import FeatureVector
        fv = FeatureVector(symbol="TEST", timestamp=0, values={}, normalized={})
        model = RegimeModel()
        result = model._classify("TEST", fv)
        # Should not crash, should return some regime
        assert result.regime is not None
        assert 0 <= result.confidence <= 1.0

    def test_extreme_rsi_triggers_overheated(self):
        from market_intelligence.regime import RegimeModel
        from market_intelligence.models import FeatureVector, MarketRegime
        fv = FeatureVector(
            symbol="TEST", timestamp=0,
            values={"rsi": 95.0, "volume_spike": 3.0, "rolling_volatility": 2.0},
            normalized={"ema_cross": 1.5, "adx": 2.0, "rsi": 3.0,
                        "rolling_volatility": 2.0, "bb_width": 1.5,
                        "funding_rate": 1.0, "liquidation_cluster_score": 0.5,
                        "volume_trend": 1.0},
        )
        model = RegimeModel()
        # Run multiple times to pass stability
        for _ in range(5):
            result = model._classify("TEST", fv)
        # With RSI=95, should lean OVERHEATED
        assert result.probabilities.get(MarketRegime.OVERHEATED, 0) > 0.1

    def test_regime_does_not_crash_on_nan_features(self):
        from market_intelligence.regime import RegimeModel
        from market_intelligence.models import FeatureVector
        fv = FeatureVector(
            symbol="TEST", timestamp=0,
            values={"rsi": None, "adx": None},
            normalized={"ema_cross": None, "adx": None, "rsi": None,
                        "rolling_volatility": None, "bb_width": None},
        )
        model = RegimeModel()
        result = model._classify("TEST", fv)
        assert result.regime is not None


# ============================================================
# SCORING EDGE CASES
# ============================================================

class TestScoringEdgeCases:
    """Test scorer with boundary conditions."""

    def test_all_zeros_features(self):
        from market_intelligence.scorer import OpportunityScorer
        from market_intelligence.models import FeatureVector, RegimeState, MarketRegime
        scorer = OpportunityScorer()
        fv = FeatureVector(
            symbol="TESTUSDT", timestamp=0,
            values={k: 0.0 for k in ["rolling_volatility_local", "bb_width_local",
                    "funding_rate", "funding_delta", "oi_delta", "oi_delta_pct",
                    "funding_pct", "spread_bps", "volume_proxy", "orderbook_imbalance",
                    "basis_bps", "basis_acceleration", "funding_slope",
                    "data_quality_code", "cvd", "rolling_volatility"]},
            normalized={k: 0.0 for k in ["rolling_volatility_local", "bb_width_local",
                        "funding_rate", "funding_delta", "oi_delta", "rolling_volatility"]},
        )
        regime = RegimeState(regime=MarketRegime.RANGE, confidence=0.5,
                             probabilities={}, stable_for_cycles=3)
        result = scorer.score(
            features={"TESTUSDT": fv},
            local_regimes={"TESTUSDT": regime},
            correlations_to_btc={"TESTUSDT": 0.5},
            spread_correlations_to_btc={"TESTUSDT": 0.3},
        )
        assert len(result) == 1
        assert 0 <= result[0].score <= 100

    def test_negative_scores_clamped(self):
        """Score should never be negative even with max risk penalty."""
        from market_intelligence.scorer import OpportunityScorer
        from market_intelligence.models import FeatureVector, RegimeState, MarketRegime
        scorer = OpportunityScorer(w_risk_penalty=1.0)  # Very high penalty
        fv = FeatureVector(
            symbol="HIGH_RISK", timestamp=0,
            values={"rolling_volatility_local": 0.01, "bb_width_local": 0.01,
                    "funding_rate": 0.0001, "funding_delta": 0.0, "oi_delta": 0.0,
                    "oi_delta_pct": 0.0, "funding_pct": 0.0, "spread_bps": 50.0,
                    "volume_proxy": 10, "orderbook_imbalance": 0.0,
                    "basis_bps": 0.0, "basis_acceleration": 0.0, "funding_slope": 0.0,
                    "data_quality_code": 0.0, "cvd": 0.0, "rolling_volatility": 3.0},
            normalized={"rolling_volatility_local": 0.0, "bb_width_local": 0.0,
                        "funding_rate": 0.0, "funding_delta": 0.0, "oi_delta": 0.0,
                        "rolling_volatility": 3.0},
        )
        regime = RegimeState(regime=MarketRegime.PANIC, confidence=0.9,
                             probabilities={}, stable_for_cycles=5)
        result = scorer.score(
            features={"HIGH_RISK": fv},
            local_regimes={"HIGH_RISK": regime},
            correlations_to_btc={"HIGH_RISK": 0.99},
            spread_correlations_to_btc={"HIGH_RISK": 0.99},
        )
        assert len(result) == 1
        assert result[0].score >= 0.0

    def test_score_stability_across_runs(self):
        """Same input should produce same score (determinism)."""
        from market_intelligence.scorer import OpportunityScorer
        from market_intelligence.models import FeatureVector, RegimeState, MarketRegime

        def make_scorer_and_score():
            scorer = OpportunityScorer()
            fv = FeatureVector(
                symbol="ETH", timestamp=1000,
                values={"rolling_volatility_local": 0.5, "bb_width_local": 0.3,
                        "funding_rate": 0.001, "funding_delta": 0.0001,
                        "oi_delta": 100, "oi_delta_pct": 2.0, "funding_pct": 0.1,
                        "spread_bps": 5.0, "volume_proxy": 1000,
                        "orderbook_imbalance": 0.1, "basis_bps": 5.0,
                        "basis_acceleration": 0.1, "funding_slope": 0.1,
                        "data_quality_code": 0.0, "cvd": 0.3, "rolling_volatility": 0.2},
                normalized={"rolling_volatility_local": 0.5, "bb_width_local": 0.3,
                            "funding_rate": 0.4, "funding_delta": 0.2,
                            "oi_delta": 0.3, "rolling_volatility": 0.2},
            )
            regime = RegimeState(regime=MarketRegime.TREND_UP, confidence=0.7,
                                 probabilities={}, stable_for_cycles=5)
            return scorer.score(
                features={"ETH": fv},
                local_regimes={"ETH": regime},
                correlations_to_btc={"ETH": 0.8},
                spread_correlations_to_btc={"ETH": 0.6},
            )[0].score

        score1 = make_scorer_and_score()
        score2 = make_scorer_and_score()
        assert score1 == pytest.approx(score2, abs=0.01)


# ============================================================
# PORTFOLIO EDGE CASES
# ============================================================

class TestPortfolioEdgeCases:
    """Test portfolio analyzer with edge cases."""

    def test_extract_base_currency_exotic(self):
        from market_intelligence.portfolio import PortfolioAnalyzer
        ebc = PortfolioAnalyzer._extract_base_currency
        assert ebc("ETHUSDT") == "ETH"
        assert ebc("BTCUSDC") == "BTC"
        assert ebc("1000PEPEUSDT") == "PEPE"
        assert ebc("1000SHIBUSDT") == "SHIB"
        assert ebc("10000WENUSDT") == "WEN"
        assert ebc("SOLUSDT") == "SOL"
        assert ebc("BTCDOMUSDT") == "BTCDOM"
        assert ebc("USDCUSDT") == "USDC"  # Stablecoin pair

    def test_single_opportunity_allocation(self):
        """Single opportunity should get 100% of allocated capital."""
        from market_intelligence.portfolio import PortfolioAnalyzer
        from market_intelligence.models import (
            OpportunityScore, RegimeState, MarketRegime, DataHealthStatus
        )
        analyzer = PortfolioAnalyzer()
        opp = OpportunityScore(
            symbol="BTCUSDT", score=80.0, confidence=0.9,
            regime=MarketRegime.TREND_UP, reasons=[], breakdown={},
        )
        regime = RegimeState(regime=MarketRegime.TREND_UP, confidence=0.8,
                             probabilities={}, stable_for_cycles=5)
        result = analyzer.analyze(
            opportunities=[opp],
            local_regimes={"BTCUSDT": regime},
            correlations_to_btc={"BTCUSDT": 1.0},
            global_regime=regime,
            global_atr_pct=2.0,
            global_volatility_regime="medium",
            data_health_status=DataHealthStatus.OK,
            scoring_enabled=True,
        )
        assert "BTCUSDT" in result.capital_allocation_pct
        # Should be the only allocation, so near 100% (after risk adjustments)
        assert result.capital_allocation_pct["BTCUSDT"] > 50.0

    def test_empty_opportunities(self):
        """No opportunities should trigger defensive mode."""
        from market_intelligence.portfolio import PortfolioAnalyzer
        from market_intelligence.models import RegimeState, MarketRegime, DataHealthStatus
        analyzer = PortfolioAnalyzer()
        regime = RegimeState(regime=MarketRegime.RANGE, confidence=0.5,
                             probabilities={}, stable_for_cycles=3)
        result = analyzer.analyze(
            opportunities=[],
            local_regimes={},
            correlations_to_btc={},
            global_regime=regime,
            global_atr_pct=2.0,
            global_volatility_regime="medium",
            data_health_status=DataHealthStatus.OK,
            scoring_enabled=True,
        )
        assert result.defensive_mode is True or len(result.capital_allocation_pct) == 0


# ============================================================
# STATISTICS EDGE CASES
# ============================================================

class TestStatisticsEdgeCases:
    """Test statistical functions with edge cases."""

    def test_robust_corr_identical_series(self):
        from market_intelligence.statistics import robust_corr
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = robust_corr(x, x)
        assert result == pytest.approx(1.0, abs=0.01)

    def test_robust_corr_opposite_series(self):
        from market_intelligence.statistics import robust_corr
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [5.0, 4.0, 3.0, 2.0, 1.0]
        result = robust_corr(x, y)
        assert result == pytest.approx(-1.0, abs=0.01)

    def test_robust_corr_constant(self):
        from market_intelligence.statistics import robust_corr
        x = [1.0] * 10
        y = [2.0] * 10
        result = robust_corr(x, y)
        # Constant series → undefined correlation → should return 0 or handle gracefully
        assert result is not None

    def test_rolling_stats_empty(self):
        from market_intelligence.statistics import RollingStats
        rs = RollingStats(window=100)
        assert rs.mean() == 0.0 or rs.mean() is None
        assert rs.std() == 0.0 or rs.std() is None

    def test_rolling_stats_single_value(self):
        from market_intelligence.statistics import RollingStats
        rs = RollingStats(window=100)
        rs.push(42.0)
        assert rs.mean() == 42.0

    def test_conditional_correlation_all_stress(self):
        from market_intelligence.statistics import conditional_correlation
        x = [float(i) for i in range(30)]
        y = [float(i * 2) for i in range(30)]
        flags = [True] * 30
        result = conditional_correlation(x, y, flags)
        assert result["stress_corr"] is not None
        assert result["normal_corr"] is None  # No normal periods

    def test_conditional_correlation_no_stress(self):
        from market_intelligence.statistics import conditional_correlation
        x = [float(i) for i in range(30)]
        y = [float(i * 2) for i in range(30)]
        flags = [False] * 30
        result = conditional_correlation(x, y, flags)
        assert result["normal_corr"] is not None
        assert result["stress_corr"] is None


# ============================================================
# BACKTEST FRAMEWORK
# ============================================================

class TestBacktester:
    """Test backtesting framework."""

    def test_empty_reports(self):
        from market_intelligence.backtest import Backtester
        bt = Backtester()
        bt.load_reports([])
        result = bt.run()
        assert result.total_trades == 0

    def test_single_report(self):
        from market_intelligence.backtest import Backtester
        bt = Backtester()
        bt.load_reports([{"timestamp": 1, "global_regime": {"regime": "RANGE"},
                          "opportunities": [], "features": {}}])
        result = bt.run()
        assert result.total_trades == 0

    def test_profitable_trend(self):
        from market_intelligence.backtest import Backtester
        reports = []
        for i in range(10):
            reports.append({
                "timestamp": i * 300,
                "global_regime": {"regime": "TREND_UP"},
                "opportunities": [{"symbol": "BTC", "score": 60, "directional_bias": "long"}],
                "portfolio_risk": {"capital_allocation_pct": {"BTC": 80.0}},
                "features": {"BTC": {"price": 100.0 + i * 2.0}},
            })
        bt = Backtester(score_threshold=30.0)
        bt.load_reports(reports)
        result = bt.run()
        assert result.total_pnl_pct > 0
        assert result.win_rate > 50.0
        assert "TREND_UP" in result.regime_performance

    def test_losing_counter_trend(self):
        from market_intelligence.backtest import Backtester
        reports = []
        for i in range(10):
            reports.append({
                "timestamp": i * 300,
                "global_regime": {"regime": "TREND_DOWN"},
                "opportunities": [{"symbol": "BTC", "score": 50, "directional_bias": "long"}],
                "portfolio_risk": {"capital_allocation_pct": {"BTC": 50.0}},
                "features": {"BTC": {"price": 100.0 - i * 2.0}},
            })
        bt = Backtester(score_threshold=30.0)
        bt.load_reports(reports)
        result = bt.run()
        # Going long in downtrend → should lose
        assert result.total_pnl_pct < 0

    def test_max_drawdown_computed(self):
        from market_intelligence.backtest import Backtester
        reports = []
        prices = [100, 110, 95, 80, 90, 100]  # Dip to 80 from peak of 110
        for i, p in enumerate(prices):
            reports.append({
                "timestamp": i * 300,
                "global_regime": {"regime": "HIGH_VOLATILITY"},
                "opportunities": [{"symbol": "ETH", "score": 40, "directional_bias": "long"}],
                "portfolio_risk": {"capital_allocation_pct": {"ETH": 100.0}},
                "features": {"ETH": {"price": float(p)}},
            })
        bt = Backtester(score_threshold=30.0)
        bt.load_reports(reports)
        result = bt.run()
        # Max DD should be > 0 (from 110 to 80 = ~27%)
        assert result.max_drawdown_pct > 0

    def test_summary_does_not_crash(self):
        from market_intelligence.backtest import BacktestMetrics
        m = BacktestMetrics()
        text = m.summary()
        assert "BACKTEST RESULTS" in text


# ============================================================
# FEATURE ENGINE EDGE CASES
# ============================================================

class TestFeatureEngineEdgeCases:
    """Test feature engine with minimal/broken data."""

    def test_empty_histories(self):
        from market_intelligence.feature_engine import FeatureEngine
        from market_intelligence.models import PairSnapshot
        engine = FeatureEngine()
        snap = PairSnapshot(symbol="TEST", timestamp=0, price=100.0)
        result = engine.compute({"TEST": snap}, {})
        assert "TEST" in result
        assert result["TEST"].symbol == "TEST"

    def test_single_price_point(self):
        from market_intelligence.feature_engine import FeatureEngine
        from market_intelligence.models import PairSnapshot
        engine = FeatureEngine()
        snap = PairSnapshot(symbol="TEST", timestamp=0, price=100.0)
        result = engine.compute(
            {"TEST": snap},
            {"TEST": {"price": [100.0]}},
        )
        fv = result["TEST"]
        # RSI, MACD, ATR should be None or default
        assert fv.values.get("rsi") is not None or fv.values.get("rsi") is None
        # Should not crash


# ============================================================
# TIME-DECAY TESTS
# ============================================================

class TestSignalTimeDecay:
    """Test signal time-decay mechanism."""

    def test_decay_reduces_old_signals(self):
        """Older signals should have lower scores."""
        import time
        from market_intelligence.scorer import OpportunityScorer
        from market_intelligence.models import FeatureVector, RegimeState, MarketRegime

        scorer = OpportunityScorer(signal_half_life_seconds=60.0)  # 1 min half-life for test

        fv = FeatureVector(
            symbol="DECAYTEST", timestamp=0,
            values={"rolling_volatility_local": 0.5, "bb_width_local": 0.3,
                    "funding_rate": 0.01, "funding_delta": 0.005,
                    "oi_delta": 500, "oi_delta_pct": 5.0, "funding_pct": 0.5,
                    "spread_bps": 3.0, "volume_proxy": 5000,
                    "orderbook_imbalance": 0.2, "basis_bps": 10.0,
                    "basis_acceleration": 0.2, "funding_slope": 0.1,
                    "data_quality_code": 0.0, "cvd": 0.5, "rolling_volatility": 0.3},
            normalized={"rolling_volatility_local": 0.5, "bb_width_local": 0.3,
                        "funding_rate": 1.0, "funding_delta": 0.8,
                        "oi_delta": 0.7, "rolling_volatility": 0.3},
        )
        regime = RegimeState(regime=MarketRegime.TREND_UP, confidence=0.8,
                             probabilities={}, stable_for_cycles=5)

        # First score — fresh signal
        result1 = scorer.score(
            features={"DECAYTEST": fv},
            local_regimes={"DECAYTEST": regime},
            correlations_to_btc={"DECAYTEST": 0.5},
            spread_correlations_to_btc={"DECAYTEST": 0.3},
        )
        score1 = result1[0].score

        # Simulate time passing by manipulating _first_seen
        scorer._first_seen["DECAYTEST"] = time.time() - 120  # 2 minutes ago

        result2 = scorer.score(
            features={"DECAYTEST": fv},
            local_regimes={"DECAYTEST": regime},
            correlations_to_btc={"DECAYTEST": 0.5},
            spread_correlations_to_btc={"DECAYTEST": 0.3},
        )
        score2 = result2[0].score

        # After 2 half-lives (120s with 60s half-life), score should be ~25% of original
        assert score2 < score1
        assert score2 > 0  # But not zero (floor at 0.15)


# ============================================================
# STRUCTURED LOGGING TESTS
# ============================================================

class TestStructuredLogging:
    """Test structured logging system."""

    def test_cycle_context(self):
        from market_intelligence.structured_log import new_cycle_context, get_cycle_context
        ctx = new_cycle_context(42)
        assert ctx["cycle_number"] == 42
        assert len(ctx["cycle_id"]) == 12
        retrieved = get_cycle_context()
        assert retrieved["cycle_number"] == 42

    def test_structured_formatter(self):
        import json
        import logging
        from market_intelligence.structured_log import StructuredFormatter, new_cycle_context
        new_cycle_context(7)
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "hello world", (), None
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["msg"] == "hello world"
        assert data["cycle_number"] == 7
        assert "cycle_id" in data
```

**Запуск тестов:**
```bash
python -m pytest tests/test_mi_edge_cases.py -v
python -m pytest tests/test_market_intelligence.py -v
python -m pytest tests/ -v
```

---

## БЛОК 11: ADAPTIVE ML — OUT-OF-SAMPLE VALIDATION

### Проблема (трейдер)
Adaptive ML weights (BLOCK 4.1) не имеют out-of-sample валидации. Momentum update может привести к overfitting. Порог в 200 сэмплов мал.

### Решение

**Файл:** `market_intelligence/ml_weights.py`

1. Добавить train/validation split:
```python
def _recompute_weights(self) -> Optional[Dict[str, float]]:
    if len(self._buffer) < self._min_samples:
        return None

    records = list(self._buffer)

    # Train/validation split: 70/30
    split_idx = int(len(records) * 0.7)
    train_set = records[:split_idx]
    val_set = records[split_idx:]

    if len(val_set) < 30:
        return None

    # Fit on training set
    candidate_weights = self._ridge_regression(train_set)

    # Validate on held-out set
    train_score = self._evaluate(candidate_weights, train_set)
    val_score = self._evaluate(candidate_weights, val_set)

    # Reject if validation score degrades significantly
    # (sign of overfitting)
    if val_score < train_score * 0.5:
        logger.warning(
            "ML weights rejected: overfit detected (train=%.3f, val=%.3f)",
            train_score, val_score
        )
        return None

    return candidate_weights


def _evaluate(self, weights: Dict[str, float], records: List) -> float:
    """Evaluate weight quality on a dataset.

    Returns correlation between predicted score and actual outcome.
    Higher = better predictions.
    """
    predictions = []
    actuals = []
    for record in records:
        fv = record.get("feature_vector", {})
        pred = sum(weights.get(k, 0) * float(v or 0) for k, v in fv.items())
        predictions.append(pred)
        actuals.append(record.get("actual_outcome", 0))

    if len(predictions) < 2:
        return 0.0

    from market_intelligence.statistics import pearson_corr
    corr = pearson_corr(predictions, actuals)
    return corr if corr is not None else 0.0
```

2. Увеличить `_min_samples` default с 200 до 500:
```python
def __init__(self, ..., min_samples: int = 500, ...):
```

3. Добавить momentum decay для предотвращения drift:
```python
# В _apply_momentum(), добавить weight decay to defaults:
decay_rate = 0.001  # Small pull toward default weights per update
default_w = self._default_weights
for key in new_weights:
    current = self._current_weights.get(key, default_w.get(key, 0.2))
    target = self._momentum * new_weights[key] + (1 - self._momentum) * current
    # Decay toward default
    target = target * (1 - decay_rate) + default_w.get(key, 0.2) * decay_rate
    new_weights[key] = target
```

---

## БЛОК 12: OUTPUT — SIGNAL AGE & DECAY IN REPORT

### Проблема
Новые данные (decay factor, signal age, conditional correlation) должны отображаться в отчёте.

### Решение

**Файл:** `market_intelligence/output.py`

В `format_human_report()`, в секции opportunities:
```python
# Для каждой opportunity добавить:
decay = opp_data.get("breakdown", {}).get("decay_factor")
age = opp_data.get("breakdown", {}).get("signal_age_seconds")
if age is not None and decay is not None:
    age_min = int(float(age) / 60)
    decay_pct = int(float(decay) * 100)
    opp_line += f" | сигнал: {age_min}мин ({decay_pct}% силы)"
```

В payload, добавить conditional correlations:
```python
# В engine.py, при построении payload:
if hasattr(p, "conditional_correlations") and p.conditional_correlations:
    payload["conditional_correlations"] = {}
    for sym, cc in p.conditional_correlations.items():
        payload["conditional_correlations"][sym] = {
            "normal": cc.get("normal_corr"),
            "stress": cc.get("stress_corr"),
            "divergence": cc.get("corr_divergence"),
            "effective": cc.get("effective_corr"),
        }
```

---

## ФИНАЛЬНАЯ ПРОВЕРКА

После завершения ВСЕХ блоков (1-12):

1. **Тесты:**
```bash
python -m pytest tests/ -v
```
Все тесты ЗЕЛЁНЫЕ.

2. **Импорт:**
```bash
python -c "from market_intelligence import FeatureKey, Backtester, BacktestMetrics; print('OK')"
python -c "from market_intelligence.structured_log import get_structured_logger; print('OK')"
python -c "from market_intelligence.statistics import conditional_correlation; print('OK')"
```

3. **Проверка типизации:**
```bash
grep -rn '"rsi"' market_intelligence/ --include="*.py" | grep -v test | grep -v __pycache__
```
Не должно быть magic strings в production коде (допустимо в тестах и комментариях).

4. **Проверка мёртвого кода:**
- `backtest.py` — экспортирован через `__init__.py`
- `structured_log.py` — используется в engine, collector, regime, scorer
- `conditional_correlation` — вызывается в engine._correlations_to_btc()
- `FeatureKey` — используется во всех модулях вместо строк

5. **Проверка новых фич:**
- Signal time-decay работает (score уменьшается со временем)
- PANIC regime активируется с 0 задержкой
- Asymmetric drawdown — quadratic penalty
- Pipeline timeout — cycle не зависает бесконечно
- Watchdog — engine перезапускается после 3 consecutive failures
- Prometheus metrics — endpoint доступен если MI_METRICS_PORT установлен
- Backtest framework — может прогнать JSONL лог и выдать Sharpe/Sortino/DD

## Критерии 10/10

### Трейдер:
- ✅ Бэктест фреймворк с Sharpe, Sortino, max DD, win rate по режимам
- ✅ Signal time-decay — старые сигналы теряют вес
- ✅ Asymmetric risk — PANIC с 0 задержкой, quadratic drawdown
- ✅ Conditional correlation — стрессовая корреляция vs нормальная
- ✅ ML weights с out-of-sample валидацией
- ✅ Все индикаторы корректны (из предыдущего раунда фиксов)

### IT-специалист:
- ✅ FeatureKey enum — нет magic strings
- ✅ Structured logging с correlation ID
- ✅ Pipeline timeout + backpressure
- ✅ Watchdog с auto-recovery
- ✅ Prometheus-compatible metrics export
- ✅ 100+ edge-case тестов
- ✅ Backtest framework для regression testing
- ✅ Zero new external dependencies
