from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


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
    ATR_SOURCE_CODE = "atr_source_code"
    ATR_PERCENTILE = "atr_percentile"

    # === Multi-Timeframe ===
    EMA_CROSS_4H = "ema_cross_4h"
    RSI_4H = "rsi_4h"
    ADX_4H = "adx_4h"
    EMA_CROSS_1D = "ema_cross_1d"
    RSI_1D = "rsi_1d"
    ADX_1D = "adx_1d"
    ADX_LOCAL = "adx_local"
    LOCAL_VOLATILITY_EXPANSION = "local_volatility_expansion"
    LOCAL_MOMENTUM_BIAS = "local_momentum_bias"

    # === Price ===
    PRICE = "price"
    EMA50 = "ema50"
    EMA200 = "ema200"
    OPEN_INTEREST = "open_interest"
    LIQUIDATION_CLUSTER = "liquidation_cluster"
    VOLATILITY_REGIME_CODE = "volatility_regime_code"

    # === Order Flow ===
    FLOW_DELTA_RATIO = "flow_delta_ratio"
    FLOW_ABSORPTION_SCORE = "flow_absorption_score"
    FLOW_DELTA_DIVERGENCE = "flow_delta_divergence"

    # === Signal Metadata (скоринг) ===
    SIGNAL_AGE_SECONDS = "signal_age_seconds"


class MarketRegime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    OVERHEATED = "overheated"
    PANIC = "panic"
    HIGH_VOLATILITY = "high_volatility"


class DataHealthStatus(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


@dataclass
class PairSnapshot:
    symbol: str
    timestamp: float
    price: float
    bid: float
    ask: float
    spot_price: float
    funding_rate: float
    open_interest: Optional[float] = None
    long_short_ratio: Optional[float] = None
    liquidation_cluster_score: Optional[float] = None
    basis: float = 0.0
    basis_acceleration: float = 0.0
    volume_proxy: Optional[float] = None
    exchange_prices: Dict[str, float] = field(default_factory=dict)
    exchange_spreads_bps: Dict[str, float] = field(default_factory=dict)
    funding_by_exchange: Dict[str, float] = field(default_factory=dict)
    data_staleness: Dict[str, float] = field(default_factory=dict)
    orderbook_imbalance: Optional[float] = None
    orderbook_bid_volume: Optional[float] = None
    orderbook_ask_volume: Optional[float] = None
    data_quality: str = "full"
    # BLOCK 1.1: Volume-weighted aggregation
    aggregation_method: str = "simple_average"
    # BLOCK 1.2: Dynamic funding normalization
    funding_interval_hours: Optional[float] = None
    # BLOCK 1.3: Orderbook imbalance improvements
    orderbook_depth_levels: Optional[int] = None
    orderbook_concentration: Optional[float] = None
    orderbook_confidence: Optional[float] = None
    # BLOCK 1.4: Slow data age tracking
    slow_data_age_seconds: Dict[str, float] = field(default_factory=dict)


@dataclass
class OHLCV:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FeatureVector:
    symbol: str
    timestamp: float
    values: Dict[str, Optional[float]]
    normalized: Dict[str, Optional[float]]


@dataclass
class RegimeState:
    regime: MarketRegime
    confidence: float
    probabilities: Dict[MarketRegime, float]
    stable_for_cycles: int
    # BLOCK 3.1: Transition regime probability
    transition_probability: float = 0.0
    # BLOCK 6: Warm start indicator (for persistence)
    warm_start: bool = False


@dataclass
class OpportunityScore:
    symbol: str
    score: float
    confidence: float
    regime: MarketRegime
    reasons: List[str]
    breakdown: Dict[str, float] = field(default_factory=dict)
    directional_bias: str = "neutral"
    # BLOCK 4.2: Multi-level signal quality
    signal_quality_level: str = "medium"  # "high", "medium", or "low"
    # BLOCK 4.3: Directional bias strength
    directional_bias_strength: float = 0.0  # Range: -1.0 to +1.0


@dataclass
class PortfolioRiskSignal:
    capital_allocation_pct: Dict[str, float]
    exposure_by_regime: Dict[MarketRegime, float]
    dynamic_risk_multiplier: Dict[str, float]
    risk_multiplier: float = 1.0
    reduced_activity: bool = False
    min_score_threshold: float = 0.0
    aggressive_mode_enabled: bool = False
    recommendation: str = ""
    defensive_mode: bool = False
    recommended_exposure_cap_pct: float = 100.0


@dataclass
class MarketIntelligenceReport:
    timestamp: float
    global_timeframe: str
    local_timeframe: str
    scoring_enabled: bool
    data_health_status: DataHealthStatus
    data_health_warnings: List[str]
    global_regime: RegimeState
    local_regimes: Dict[str, RegimeState]
    opportunities: List[OpportunityScore]
    portfolio_risk: PortfolioRiskSignal
    extreme_alerts: List[str]
    dynamic_deltas: Dict
    payload: Dict

    @property
    def iso_time(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.timestamp))
