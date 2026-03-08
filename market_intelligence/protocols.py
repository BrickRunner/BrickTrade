"""Abstract protocols for exchange adapters and pluggable components."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class NormalizedOrderbook:
    """Exchange-agnostic orderbook snapshot."""
    bids: List[List[float]]   # [[price, qty], ...] sorted desc by price
    asks: List[List[float]]   # [[price, qty], ...] sorted asc by price
    timestamp: float
    exchange: str
    symbol: str


@dataclass(frozen=True)
class NormalizedTrade:
    """Single trade from tape."""
    price: float
    quantity: float
    side: str          # "buy" or "sell"
    timestamp: float
    is_maker: bool     # True if maker (passive/limit), False if taker (aggressive/market)


@dataclass(frozen=True)
class OrderbookDepthMetrics:
    """Computed depth metrics from orderbook."""
    bid_volume_total: float
    ask_volume_total: float
    imbalance: float                    # (bid - ask) / (bid + ask), range [-1, 1]
    bid_depth_weighted_price: float     # volume-weighted avg bid price
    ask_depth_weighted_price: float     # volume-weighted avg ask price
    spread_bps: float                   # (best_ask - best_bid) / mid * 10000
    concentration_top3_bid: float       # % of total bid vol in top 3 levels
    concentration_top3_ask: float       # % of total ask vol in top 3 levels
    levels_count: int


@runtime_checkable
class ExchangeAdapter(Protocol):
    """Protocol that any exchange REST client must implement for MI system."""

    @property
    def exchange_name(self) -> str: ...

    async def get_orderbook(self, symbol: str, levels: int = 20) -> Optional[NormalizedOrderbook]: ...

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> Optional[List[NormalizedTrade]]: ...

    async def get_funding_rate(self, symbol: str) -> Optional[float]: ...

    async def get_open_interest(self, symbol: str) -> Optional[float]: ...

    async def get_ohlcv(self, symbol: str, interval: str, limit: int = 100) -> Optional[List[Dict[str, float]]]: ...


@runtime_checkable
class ScoringStrategy(Protocol):
    """Pluggable scoring strategy for opportunity evaluation."""

    def score(
        self,
        features: Dict[str, Any],
        regime: Any,
        correlations: Dict[str, float],
        volumes: Dict[str, float],
    ) -> List[Any]: ...


class MetricsCollector(abc.ABC):
    """Abstract base for observability metrics collection."""

    @abc.abstractmethod
    def record_latency(self, stage: str, duration_ms: float) -> None: ...

    @abc.abstractmethod
    def record_counter(self, name: str, value: int = 1, labels: Optional[Dict[str, str]] = None) -> None: ...

    @abc.abstractmethod
    def record_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None: ...

    @abc.abstractmethod
    def get_snapshot(self) -> Dict[str, Any]: ...
