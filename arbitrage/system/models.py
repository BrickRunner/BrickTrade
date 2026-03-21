from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StrategyId(str, Enum):
    FUTURES_CROSS_EXCHANGE = "futures_cross_exchange"


@dataclass(frozen=True)
class OrderBookSnapshot:
    exchange: str
    symbol: str
    bid: float
    ask: float
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    orderbooks: Dict[str, OrderBookSnapshot]
    spot_orderbooks: Dict[str, OrderBookSnapshot]
    orderbook_depth: Dict[str, Dict[str, list]]
    spot_orderbook_depth: Dict[str, Dict[str, list]]
    balances: Dict[str, float]
    fee_bps: Dict[str, Dict[str, float]]
    funding_rates: Dict[str, float]
    volatility: float
    trend_strength: float
    atr: float
    atr_rolling: float
    indicators: Dict[str, float]
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TradeIntent:
    strategy_id: StrategyId
    symbol: str
    long_exchange: str
    short_exchange: str
    side: str
    confidence: float
    expected_edge_bps: float
    stop_loss_bps: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AllocationPlan:
    strategy_allocations: Dict[StrategyId, float]
    total_allocatable_capital: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class OpenPosition:
    position_id: str
    strategy_id: StrategyId
    symbol: str
    long_exchange: str
    short_exchange: str
    notional_usd: float
    entry_mid: float
    stop_loss_bps: float
    opened_at: float = field(default_factory=time.time)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionReport:
    success: bool
    position_id: Optional[str]
    fill_price_long: float
    fill_price_short: float
    notional_usd: float
    slippage_bps: float
    message: str
    hedged: bool = False


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    kill_switch_triggered: bool = False
