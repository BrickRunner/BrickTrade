from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _as_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MarketIntelligenceConfig:
    enabled: bool
    exchanges: List[str]
    symbols: List[str]
    max_symbols: int
    interval_seconds: int
    startup_report_enabled: bool
    hourly_report_enabled: bool
    event_report_enabled: bool
    min_regime_duration_cycles: int
    confidence_threshold: float
    smoothing_alpha: float
    feature_window: int
    zscore_window: int
    correlation_window: int
    stress_correlation_window: int
    historical_window: int
    adaptive_ml_weighting: bool
    order_flow_enabled: bool
    global_timeframe: str
    local_timeframe: str
    min_opportunity_score: float
    log_dir: str
    jsonl_file_name: str

    # Scoring weights (must sum to ~1.0 before risk_penalty)
    score_weight_volatility: float
    score_weight_funding: float
    score_weight_oi: float
    score_weight_regime: float
    score_weight_risk_penalty: float
    score_weight_liquidity: float

    # Persistence
    persist_enabled: bool
    persist_file: str
    persist_every_n_cycles: int

    # Regime logit coefficients
    regime_ema_cross_coef: float
    regime_adx_coef: float
    regime_range_ema_coef: float
    regime_range_adx_coef: float
    regime_rsi_overheat_coef: float
    regime_rsi_panic_coef: float
    regime_vol_coef: float
    regime_bb_coef: float
    regime_interaction_strength: float

    # Alert thresholds
    alert_rsi_overheat: float
    alert_rsi_panic_vol: float
    alert_funding_extreme: float

    # Regime interaction thresholds
    regime_blowoff_adx: float
    regime_blowoff_rsi: float
    regime_capitulation_adx: float
    regime_capitulation_rsi: float

    # Multi-timeframe analysis
    mtf_enabled: bool
    mtf_timeframes: List[str]

    # Structured logging
    structured_logging: bool

    # Signal time-decay
    signal_half_life_seconds: float

    @classmethod
    def from_env(cls) -> "MarketIntelligenceConfig":
        exchanges = [x.strip().lower() for x in os.getenv("MI_EXCHANGES", os.getenv("EXCHANGES", "okx,htx")).split(",") if x.strip()]

        raw_symbols = os.getenv("MI_SYMBOLS", os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"))
        if raw_symbols.strip().upper() in {"ALL", "*", "AUTO"}:
            symbols = []
        else:
            symbols = [x.strip().upper() for x in raw_symbols.split(",") if x.strip()]

        return cls(
            enabled=_as_bool("MI_ENABLED", True),
            exchanges=exchanges,
            symbols=symbols,
            max_symbols=max(1, _as_int("MI_MAX_SYMBOLS", 24)),
            interval_seconds=max(30, _as_int("MI_INTERVAL_SECONDS", 300)),
            startup_report_enabled=_as_bool("MI_STARTUP_REPORT", True),
            hourly_report_enabled=_as_bool("MI_HOURLY_REPORT", True),
            event_report_enabled=_as_bool("MI_EVENT_REPORT", True),
            min_regime_duration_cycles=max(1, _as_int("MI_MIN_REGIME_DURATION", 2)),
            confidence_threshold=max(0.3, min(0.95, _as_float("MI_CONFIDENCE_THRESHOLD", 0.55))),
            smoothing_alpha=max(0.05, min(1.0, _as_float("MI_SMOOTHING_ALPHA", 0.35))),
            feature_window=max(30, _as_int("MI_FEATURE_WINDOW", 120)),
            zscore_window=max(30, _as_int("MI_ZSCORE_WINDOW", 180)),
            correlation_window=max(30, _as_int("MI_CORRELATION_WINDOW", 180)),
            stress_correlation_window=max(5, _as_int("MI_STRESS_CORRELATION_WINDOW", 60)),
            historical_window=max(60, _as_int("MI_HISTORICAL_WINDOW", 720)),
            adaptive_ml_weighting=_as_bool("MI_ADAPTIVE_ML_WEIGHTING", False),
            order_flow_enabled=_as_bool("MI_ORDER_FLOW_ENABLED", False),
            global_timeframe=os.getenv("MI_GLOBAL_TIMEFRAME", "1H"),
            local_timeframe=os.getenv("MI_LOCAL_TIMEFRAME", "5M"),
            min_opportunity_score=max(0.0, min(100.0, _as_float("MI_MIN_OPPORTUNITY_SCORE", 20.0))),
            log_dir=os.getenv("MI_LOG_DIR", "logs"),
            jsonl_file_name=os.getenv("MI_JSONL_FILE", "market_intelligence.jsonl"),
            # Persistence
            persist_enabled=_as_bool("MI_PERSIST_ENABLED", True),
            persist_file=os.getenv("MI_PERSIST_FILE", "logs/mi_state.json"),
            persist_every_n_cycles=max(1, _as_int("MI_PERSIST_EVERY_N_CYCLES", 5)),
            # Scoring weights
            score_weight_volatility=_as_float("MI_SCORE_W_VOLATILITY", 0.26),
            score_weight_funding=_as_float("MI_SCORE_W_FUNDING", 0.24),
            score_weight_oi=_as_float("MI_SCORE_W_OI", 0.20),
            score_weight_regime=_as_float("MI_SCORE_W_REGIME", 0.30),
            score_weight_risk_penalty=_as_float("MI_SCORE_W_RISK_PENALTY", 0.28),
            score_weight_liquidity=_as_float("MI_SCORE_W_LIQUIDITY", 0.15),
            # Regime logit coefficients
            regime_ema_cross_coef=_as_float("MI_REGIME_EMA_CROSS_COEF", 1.2),
            regime_adx_coef=_as_float("MI_REGIME_ADX_COEF", 0.8),
            regime_range_ema_coef=_as_float("MI_REGIME_RANGE_EMA_COEF", 1.1),
            regime_range_adx_coef=_as_float("MI_REGIME_RANGE_ADX_COEF", 0.7),
            regime_rsi_overheat_coef=_as_float("MI_REGIME_RSI_OVERHEAT_COEF", 1.0),
            regime_rsi_panic_coef=_as_float("MI_REGIME_RSI_PANIC_COEF", 1.0),
            regime_vol_coef=_as_float("MI_REGIME_VOL_COEF", 0.9),
            regime_bb_coef=_as_float("MI_REGIME_BB_COEF", 0.8),
            regime_interaction_strength=_as_float("MI_REGIME_INTERACTION_STRENGTH", 1.0),
            # Alert thresholds
            alert_rsi_overheat=_as_float("MI_ALERT_RSI_OVERHEAT", 75.0),
            alert_rsi_panic_vol=_as_float("MI_ALERT_RSI_PANIC_VOL", 0.01),
            alert_funding_extreme=_as_float("MI_ALERT_FUNDING_EXTREME", 0.001),
            # Regime interaction thresholds
            regime_blowoff_adx=_as_float("MI_REGIME_BLOWOFF_ADX", 35.0),
            regime_blowoff_rsi=_as_float("MI_REGIME_BLOWOFF_RSI", 75.0),
            regime_capitulation_adx=_as_float("MI_REGIME_CAPITULATION_ADX", 30.0),
            regime_capitulation_rsi=_as_float("MI_REGIME_CAPITULATION_RSI", 25.0),
            # Multi-timeframe analysis
            mtf_enabled=_as_bool("MI_MTF_ENABLED", False),
            mtf_timeframes=[t.strip() for t in os.getenv("MI_MTF_TIMEFRAMES", "1H,4H").split(",") if t.strip()],
            # Structured logging
            structured_logging=_as_bool("MI_STRUCTURED_LOGGING", True),
            # Signal time-decay
            signal_half_life_seconds=_as_float("MI_SIGNAL_HALF_LIFE", 1800.0),
        )
