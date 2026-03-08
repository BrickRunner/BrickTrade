from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


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


@dataclass
class OpportunityScore:
    symbol: str
    score: float
    confidence: float
    regime: MarketRegime
    reasons: List[str]
    breakdown: Dict[str, float] = field(default_factory=dict)
    directional_bias: str = "neutral"


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
