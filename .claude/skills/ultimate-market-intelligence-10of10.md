---
name: ultimate-market-intelligence-10of10
description: "Transform Market Intelligence System to undisputed 10/10 from both professional trading and elite IT perspectives. Covers: order flow engine, funding mean-reversion, liquidation cascades, market microstructure, advanced ML, backtesting framework, ABC/Protocol architecture, observability, concurrency fixes, persistence versioning, and 95%+ test coverage."
---

# ULTIMATE Market Intelligence Upgrade — 10/10 Professional Grade

You are performing a COMPREHENSIVE upgrade of the Market Intelligence System in `market_intelligence/` to elite professional quality — the kind that would impress any quantitative trader and any senior software architect.

---

## MANDATORY PRE-WORK

**Read ALL files before making ANY changes:**

1. ALL files in `market_intelligence/` (every .py file)
2. `arbitrage/core/market_data.py`
3. `arbitrage/exchanges/okx_rest.py`, `arbitrage/exchanges/htx_rest.py`
4. `tests/test_market_intelligence.py`
5. `requirements.txt`

Understand the full pipeline: `collector → feature_engine → regime → scorer → portfolio → engine → output`

**CONSTRAINTS (non-negotiable):**
- Do NOT change the public API of `MarketIntelligenceEngine.run_once()` or `MarketIntelligenceReport`
- Do NOT change `.env` variable names or add required new env vars (all new params must have sensible defaults)
- Do NOT add external dependencies — use ONLY what's in `requirements.txt` (no numpy, no pandas, no scipy, no sklearn)
- Keep all changes backward-compatible with existing config
- Preserve all existing logging
- All new code must pass `python -m pytest tests/test_market_intelligence.py -v`
- NEVER break existing tests. If a test fails after your change, read the test first — decide if the test or your code is wrong
- Use type hints everywhere. Use `from __future__ import annotations` for modern syntax
- Follow existing code style (dataclasses, Optional with `or 0.0` fallbacks)

---

## PHASE 1: EXCHANGE ADAPTER PROTOCOL (Architecture Foundation)

### 1.1 Create abstract exchange protocol

**New file:** `market_intelligence/protocols.py`

```python
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
```

This establishes the contract that any exchange adapter must follow.

---

## PHASE 2: OBSERVABILITY & METRICS ENGINE

### 2.1 In-process metrics collector

**New file:** `market_intelligence/metrics.py`

```python
"""Lightweight in-process metrics collection for observability.
No external dependencies — stores metrics in memory with ring buffers."""
from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class LatencyRecord:
    stage: str
    duration_ms: float
    timestamp: float


class InProcessMetrics:
    """Thread-safe metrics collector with ring buffers.

    Tracks:
    - Pipeline stage latencies (collection, features, regime, scoring, portfolio, total)
    - Exchange API call counts and error rates
    - Regime transitions
    - Data quality degradation events
    - Scoring distribution statistics
    """

    def __init__(self, history_size: int = 500):
        self._lock = threading.Lock()
        self._history_size = history_size

        # Latency tracking per stage
        self._latencies: Dict[str, Deque[Tuple[float, float]]] = {}  # stage -> deque of (timestamp, ms)

        # Counters
        self._counters: Dict[str, int] = {}

        # Gauges (current values)
        self._gauges: Dict[str, float] = {}

        # Event log (ring buffer)
        self._events: Deque[Tuple[float, str, Dict[str, Any]]] = deque(maxlen=history_size)

        # Per-exchange error tracking
        self._exchange_calls: Dict[str, int] = {}
        self._exchange_errors: Dict[str, int] = {}

    def record_latency(self, stage: str, duration_ms: float) -> None:
        with self._lock:
            if stage not in self._latencies:
                self._latencies[stage] = deque(maxlen=self._history_size)
            self._latencies[stage].append((time.time(), duration_ms))

    def record_counter(self, name: str, value: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        key = name if not labels else f"{name}|{'|'.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def record_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = name if not labels else f"{name}|{'|'.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
        with self._lock:
            self._gauges[key] = value

    def record_event(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._events.append((time.time(), event_type, details or {}))

    def record_exchange_call(self, exchange: str, success: bool) -> None:
        with self._lock:
            self._exchange_calls[exchange] = self._exchange_calls.get(exchange, 0) + 1
            if not success:
                self._exchange_errors[exchange] = self._exchange_errors.get(exchange, 0) + 1

    def get_latency_stats(self, stage: str, last_n: int = 50) -> Dict[str, float]:
        """Return min/max/avg/p95 latency for a stage."""
        with self._lock:
            records = self._latencies.get(stage, deque())
            if not records:
                return {"min_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0, "p95_ms": 0.0, "count": 0}
            values = [r[1] for r in list(records)[-last_n:]]

        values.sort()
        n = len(values)
        p95_idx = min(n - 1, int(n * 0.95))
        return {
            "min_ms": values[0],
            "max_ms": values[-1],
            "avg_ms": sum(values) / n,
            "p95_ms": values[p95_idx],
            "count": n,
        }

    def get_exchange_health(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            result = {}
            for ex in set(list(self._exchange_calls.keys()) + list(self._exchange_errors.keys())):
                calls = self._exchange_calls.get(ex, 0)
                errors = self._exchange_errors.get(ex, 0)
                result[ex] = {
                    "total_calls": calls,
                    "errors": errors,
                    "error_rate": errors / max(calls, 1),
                    "healthy": (errors / max(calls, 1)) < 0.3,
                }
            return result

    def get_snapshot(self) -> Dict[str, Any]:
        """Full metrics snapshot for health check / Telegram report."""
        stages = ["collection", "features", "regime", "scoring", "portfolio", "total"]
        latency_summary = {s: self.get_latency_stats(s) for s in stages if s in self._latencies}
        with self._lock:
            return {
                "latencies": latency_summary,
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "exchange_health": self.get_exchange_health(),
                "recent_events": [
                    {"ts": ts, "type": t, "details": d}
                    for ts, t, d in list(self._events)[-20:]
                ],
            }


# Global singleton
_metrics: Optional[InProcessMetrics] = None


def get_metrics() -> InProcessMetrics:
    global _metrics
    if _metrics is None:
        _metrics = InProcessMetrics()
    return _metrics
```

### 2.2 Instrument the engine with latency tracking

**File:** `market_intelligence/engine.py`

In `run_once()`, wrap each major stage with timing. Import `get_metrics` from `market_intelligence.metrics`.

Add at the top of `run_once()`:
```python
from market_intelligence.metrics import get_metrics
metrics = get_metrics()
cycle_start = time.time()
```

After each stage, record latency:
```python
# After collector.collect()
t_collect = time.time()
metrics.record_latency("collection", (t_collect - cycle_start) * 1000)

# After _compute_pipeline()
t_features = time.time()
metrics.record_latency("features", (t_features - t_collect) * 1000)

# After regime classification (inside _compute_pipeline or after)
t_regime = time.time()
metrics.record_latency("regime", (t_regime - t_features) * 1000)

# After scoring
t_scoring = time.time()
metrics.record_latency("scoring", (t_scoring - t_regime) * 1000)

# After portfolio analysis
t_portfolio = time.time()
metrics.record_latency("portfolio", (t_portfolio - t_scoring) * 1000)

# At the end
metrics.record_latency("total", (time.time() - cycle_start) * 1000)
```

Record events for regime transitions:
```python
if prev_global_regime and global_regime.regime != prev_global_regime.regime:
    metrics.record_event("regime_transition", {
        "from": prev_global_regime.regime.name,
        "to": global_regime.regime.name,
        "confidence": global_regime.confidence,
    })
```

Record gauges for key values:
```python
metrics.record_gauge("symbols_active", len(symbols))
metrics.record_gauge("scoring_enabled", 1.0 if scoring_enabled else 0.0)
metrics.record_gauge("global_confidence", global_regime.confidence)
metrics.record_gauge("risk_multiplier", portfolio_result.risk_multiplier)
```

### 2.3 Add metrics to health check

**File:** `market_intelligence/service.py`, method `health_check()`

Add to the returned dict:
```python
from market_intelligence.metrics import get_metrics
result["metrics"] = get_metrics().get_snapshot()
```

### 2.4 Add performance summary to Telegram report

**File:** `market_intelligence/output.py`

In `format_human_report()`, after the risk section, add a compact performance line:
```python
from market_intelligence.metrics import get_metrics
m = get_metrics()
total_stats = m.get_latency_stats("total")
if total_stats["count"] > 0:
    lines.append(f"\n⏱ Цикл: {total_stats['avg_ms']:.0f}ms (p95: {total_stats['p95_ms']:.0f}ms)")
    ex_health = m.get_exchange_health()
    unhealthy = [ex for ex, h in ex_health.items() if not h["healthy"]]
    if unhealthy:
        lines.append(f"⚠ Проблемы с биржами: {', '.join(unhealthy)}")
```

---

## PHASE 3: ORDER FLOW ENGINE (Critical Trading Feature)

### 3.1 Order flow analyzer module

**New file:** `market_intelligence/order_flow.py`

```python
"""Order flow analysis engine.

Provides:
- Delta profiles (aggressive buy vs sell volume per price level)
- Absorption detection (large passive orders absorbing aggression)
- Aggressive flow tracking (market order dominance)
- Footprint-like delta divergence (price up + negative delta = weakness)

All computed from trade tape and orderbook snapshots.
No external dependencies.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class DeltaProfile:
    """Aggregated buy/sell delta over a time window."""
    total_buy_volume: float = 0.0
    total_sell_volume: float = 0.0
    net_delta: float = 0.0                # buy - sell
    delta_ratio: float = 0.0              # net_delta / total_volume, range [-1, 1]
    aggressive_buy_pct: float = 0.0       # % of buys that are taker (aggressive)
    aggressive_sell_pct: float = 0.0      # % of sells that are taker
    absorption_score: float = 0.0         # 0-1, how much passive volume absorbs aggression
    large_trade_bias: float = 0.0         # bias from trades > 2x avg size
    delta_divergence: bool = False        # price moved up but delta negative (or vice versa)
    divergence_strength: float = 0.0      # 0-1 strength of divergence signal


@dataclass
class AbsorptionEvent:
    """Detected absorption at a price level."""
    price: float
    side: str          # "bid" (buyers absorbing sells) or "ask" (sellers absorbing buys)
    absorbed_volume: float
    price_held: bool   # did price hold after absorption?
    timestamp: float


@dataclass
class OrderFlowState:
    """Persistent state for order flow analysis per symbol."""
    trades: Deque[Tuple[float, float, float, str, bool]]  # (ts, price, qty, side, is_maker)
    orderbook_snapshots: Deque[Tuple[float, List[List[float]], List[List[float]]]]  # (ts, bids, asks)
    delta_history: Deque[float]                # net delta per interval
    absorption_events: Deque[AbsorptionEvent]
    last_price: float = 0.0

    def __init__(self, maxlen: int = 500):
        self.trades = deque(maxlen=maxlen * 10)  # trades are high frequency
        self.orderbook_snapshots = deque(maxlen=maxlen)
        self.delta_history = deque(maxlen=maxlen)
        self.absorption_events = deque(maxlen=50)
        self.last_price = 0.0


class OrderFlowAnalyzer:
    """Analyzes trade tape and orderbook snapshots for order flow signals.

    Usage:
        analyzer = OrderFlowAnalyzer()
        analyzer.push_trades("BTCUSDT", trades)
        analyzer.push_orderbook("BTCUSDT", bids, asks)
        profile = analyzer.compute_delta_profile("BTCUSDT", window_seconds=300)
    """

    def __init__(self, large_trade_multiplier: float = 2.0):
        self._states: Dict[str, OrderFlowState] = {}
        self._large_trade_mult = large_trade_multiplier

    def _get_state(self, symbol: str) -> OrderFlowState:
        if symbol not in self._states:
            self._states[symbol] = OrderFlowState()
        return self._states[symbol]

    def push_trades(self, symbol: str, trades: List[Tuple[float, float, float, str, bool]]) -> None:
        """Push trades as (timestamp, price, quantity, side, is_maker)."""
        state = self._get_state(symbol)
        for t in trades:
            state.trades.append(t)
        if trades:
            state.last_price = trades[-1][1]

    def push_orderbook(self, symbol: str, bids: List[List[float]], asks: List[List[float]]) -> None:
        """Push an orderbook snapshot for absorption detection."""
        state = self._get_state(symbol)
        state.orderbook_snapshots.append((time.time(), bids, asks))

    def compute_delta_profile(self, symbol: str, window_seconds: float = 300.0) -> DeltaProfile:
        """Compute aggregated delta profile over last `window_seconds`."""
        state = self._get_state(symbol)
        if not state.trades:
            return DeltaProfile()

        cutoff = time.time() - window_seconds
        recent = [(ts, p, q, s, m) for ts, p, q, s, m in state.trades if ts >= cutoff]
        if not recent:
            return DeltaProfile()

        buy_vol = 0.0
        sell_vol = 0.0
        aggressive_buy = 0.0
        aggressive_sell = 0.0
        large_buy = 0.0
        large_sell = 0.0

        # Compute average trade size for large trade detection
        avg_qty = sum(q for _, _, q, _, _ in recent) / len(recent) if recent else 1.0
        large_threshold = avg_qty * self._large_trade_mult

        for ts, price, qty, side, is_maker in recent:
            if side == "buy":
                buy_vol += qty
                if not is_maker:  # taker = aggressive
                    aggressive_buy += qty
                if qty >= large_threshold:
                    large_buy += qty
            else:
                sell_vol += qty
                if not is_maker:
                    aggressive_sell += qty
                if qty >= large_threshold:
                    large_sell += qty

        total_vol = buy_vol + sell_vol
        net_delta = buy_vol - sell_vol
        delta_ratio = net_delta / total_vol if total_vol > 0 else 0.0

        agg_buy_pct = aggressive_buy / max(buy_vol, 1e-12)
        agg_sell_pct = aggressive_sell / max(sell_vol, 1e-12)

        # Large trade bias: positive = large buyers dominate
        large_total = large_buy + large_sell
        large_bias = (large_buy - large_sell) / large_total if large_total > 0 else 0.0

        # Absorption detection
        absorption = self._detect_absorption(state, window_seconds)

        # Delta divergence: price direction vs delta direction disagree
        price_change = 0.0
        if len(recent) >= 2:
            price_change = recent[-1][1] - recent[0][1]

        divergence = False
        div_strength = 0.0
        if abs(price_change) > 0 and abs(net_delta) > 0:
            price_dir = 1.0 if price_change > 0 else -1.0
            delta_dir = 1.0 if net_delta > 0 else -1.0
            if price_dir != delta_dir:
                divergence = True
                # Strength: how strong is the divergence?
                div_strength = min(1.0, abs(delta_ratio) * 2.0)

        # Record delta for history
        state.delta_history.append(net_delta)

        return DeltaProfile(
            total_buy_volume=buy_vol,
            total_sell_volume=sell_vol,
            net_delta=net_delta,
            delta_ratio=delta_ratio,
            aggressive_buy_pct=agg_buy_pct,
            aggressive_sell_pct=agg_sell_pct,
            absorption_score=absorption,
            large_trade_bias=large_bias,
            delta_divergence=divergence,
            divergence_strength=div_strength,
        )

    def _detect_absorption(self, state: OrderFlowState, window_seconds: float) -> float:
        """Detect absorption patterns from orderbook + trade data.

        Absorption = large passive volume that prevents price from moving.
        Score 0-1 where 1 = strong absorption detected.
        """
        if len(state.orderbook_snapshots) < 2:
            return 0.0

        cutoff = time.time() - window_seconds
        snapshots = [(ts, b, a) for ts, b, a in state.orderbook_snapshots if ts >= cutoff]
        if len(snapshots) < 2:
            return 0.0

        # Compare first and last snapshot: if top-of-book volume increased
        # while aggressive volume hit it, that's absorption
        first_ts, first_bids, first_asks = snapshots[0]
        last_ts, last_bids, last_asks = snapshots[-1]

        if not first_bids or not last_bids or not first_asks or not last_asks:
            return 0.0

        # Check if bid wall absorbed selling
        first_top_bid_vol = first_bids[0][1] if first_bids else 0.0
        last_top_bid_vol = last_bids[0][1] if last_bids else 0.0
        first_top_bid_price = first_bids[0][0] if first_bids else 0.0
        last_top_bid_price = last_bids[0][0] if last_bids else 0.0

        bid_absorption = 0.0
        if abs(last_top_bid_price - first_top_bid_price) < first_top_bid_price * 0.001:
            # Price level held
            if last_top_bid_vol >= first_top_bid_vol * 0.5:
                # Volume was replenished (absorbed and refilled)
                bid_absorption = min(1.0, last_top_bid_vol / max(first_top_bid_vol, 1e-12))

        # Check if ask wall absorbed buying
        first_top_ask_vol = first_asks[0][1] if first_asks else 0.0
        last_top_ask_vol = last_asks[0][1] if last_asks else 0.0
        first_top_ask_price = first_asks[0][0] if first_asks else 0.0
        last_top_ask_price = last_asks[0][0] if last_asks else 0.0

        ask_absorption = 0.0
        if abs(last_top_ask_price - first_top_ask_price) < first_top_ask_price * 0.001:
            if last_top_ask_vol >= first_top_ask_vol * 0.5:
                ask_absorption = min(1.0, last_top_ask_vol / max(first_top_ask_vol, 1e-12))

        return max(bid_absorption, ask_absorption)

    def get_flow_features(self, symbol: str, window_seconds: float = 300.0) -> Dict[str, Optional[float]]:
        """Return order flow features for integration with feature engine."""
        profile = self.compute_delta_profile(symbol, window_seconds)
        state = self._get_state(symbol)

        # Delta momentum: is delta accelerating?
        delta_momentum = 0.0
        if len(state.delta_history) >= 5:
            recent_5 = list(state.delta_history)[-5:]
            recent_3 = recent_5[-3:]
            older_2 = recent_5[:2]
            avg_recent = sum(recent_3) / 3
            avg_older = sum(older_2) / 2
            if abs(avg_older) > 1e-12:
                delta_momentum = (avg_recent - avg_older) / abs(avg_older)
            delta_momentum = max(-1.0, min(1.0, delta_momentum))

        return {
            "flow_net_delta": profile.net_delta,
            "flow_delta_ratio": profile.delta_ratio,
            "flow_aggressive_buy_pct": profile.aggressive_buy_pct,
            "flow_aggressive_sell_pct": profile.aggressive_sell_pct,
            "flow_absorption_score": profile.absorption_score,
            "flow_large_trade_bias": profile.large_trade_bias,
            "flow_delta_divergence": 1.0 if profile.delta_divergence else 0.0,
            "flow_divergence_strength": profile.divergence_strength,
            "flow_delta_momentum": delta_momentum,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state for persistence."""
        result = {}
        for sym, state in self._states.items():
            result[sym] = {
                "delta_history": list(state.delta_history),
                "last_price": state.last_price,
            }
        return result

    def restore_from_dict(self, data: Dict[str, Any]) -> None:
        """Restore state from persisted data."""
        for sym, d in data.items():
            state = self._get_state(sym)
            for v in d.get("delta_history", []):
                state.delta_history.append(v)
            state.last_price = d.get("last_price", 0.0)
```

### 3.2 Integrate order flow into collector

**File:** `market_intelligence/collector.py`

Import `OrderFlowAnalyzer` and add as `self._order_flow = OrderFlowAnalyzer()` in `__init__`.

Add a method to fetch recent trades from exchanges:
```python
async def _update_order_flow(self, symbols: List[str]) -> None:
    """Fetch recent trades from exchanges and feed to order flow analyzer."""
    for symbol in symbols:
        for ex in self.exchanges:
            cb = self._circuit_breakers.get(ex)
            if cb and not cb.is_available():
                continue
            try:
                limiters = get_exchange_rate_limiters()
                await limiters.get(ex).acquire()
                trades = await self.market_data.fetch_recent_trades(ex, symbol, limit=100)
                if trades:
                    # Convert to (ts, price, qty, side, is_maker)
                    parsed = []
                    for t in trades:
                        parsed.append((
                            float(t.get("timestamp", time.time())),
                            float(t["price"]),
                            float(t["quantity"]),
                            t["side"],
                            t.get("is_maker", False),
                        ))
                    self._order_flow.push_trades(symbol, parsed)
                if cb:
                    cb.record_success()
            except Exception as e:
                if cb:
                    cb.record_failure()
                logger.warning("Order flow %s/%s: %s", ex, symbol, e)
```

**IMPORTANT**: Check if `market_data.fetch_recent_trades()` exists. If not, add it to `MarketDataEngine` in `arbitrage/core/market_data.py`:

```python
async def fetch_recent_trades(self, exchange: str, symbol: str, limit: int = 100) -> Optional[List[Dict[str, Any]]]:
    """Fetch recent trades. Returns list of {price, quantity, side, timestamp, is_maker} or None."""
    try:
        if exchange == "okx":
            # OKX: GET /api/v5/market/trades?instId=BTC-USDT-SWAP&limit=100
            inst_id = self._to_okx_symbol(symbol)
            async with self._session.get(
                f"https://www.okx.com/api/v5/market/trades",
                params={"instId": inst_id, "limit": str(limit)}
            ) as resp:
                data = await resp.json()
                if data.get("code") == "0" and data.get("data"):
                    return [
                        {
                            "price": float(t["px"]),
                            "quantity": float(t["sz"]),
                            "side": t["side"],
                            "timestamp": float(t["ts"]) / 1000.0,
                            "is_maker": False,  # OKX doesn't provide maker flag in public trades
                        }
                        for t in data["data"]
                    ]
        elif exchange == "htx":
            # HTX: GET /linear-swap-ex/market/trade?contract_code=BTC-USDT&size=100
            htx_sym = self._to_htx_symbol(symbol)
            async with self._session.get(
                f"https://api.hbdm.com/linear-swap-ex/market/trade",
                params={"contract_code": htx_sym, "size": str(limit)}
            ) as resp:
                data = await resp.json()
                if data.get("status") == "ok" and data.get("tick", {}).get("data"):
                    return [
                        {
                            "price": float(t["price"]),
                            "quantity": float(t["amount"]),
                            "side": "buy" if t["direction"] == "buy" else "sell",
                            "timestamp": float(t["ts"]) / 1000.0,
                            "is_maker": False,
                        }
                        for t in data["tick"]["data"]
                    ]
    except Exception as e:
        logger.warning("fetch_recent_trades %s/%s: %s", exchange, symbol, e)
    return None
```

Check the actual REST client methods and API endpoint formats before implementing — use the patterns already in `market_data.py`.

Call `_update_order_flow(symbols)` in the `collect()` method, after existing data collection.

### 3.3 Feed order flow features into feature engine

**File:** `market_intelligence/feature_engine.py`

After computing all existing features, merge order flow features:
```python
# Order flow features (from collector's OrderFlowAnalyzer)
if hasattr(collector, '_order_flow'):
    flow_features = collector._order_flow.get_flow_features(symbol, window_seconds=300.0)
    for key, val in flow_features.items():
        values[key] = val
        if val is not None and key in self._rolling:
            self._rolling[key].push(val)
            normalized[key] = self._rolling[key].z_score(val)
```

### 3.4 Use order flow in regime classification

**File:** `market_intelligence/regime.py`, method `_classify`

After existing orderbook pressure block, add:
```python
# Order flow: delta divergence weakens trend conviction
flow_divergence = float(v.get("flow_delta_divergence") or 0.0)
flow_div_strength = float(v.get("flow_divergence_strength") or 0.0)
if flow_divergence > 0.5 and flow_div_strength > 0.3:
    # Price and delta disagree — trend may reverse
    logits[MarketRegime.TREND_UP] -= 0.15 * flow_div_strength
    logits[MarketRegime.TREND_DOWN] -= 0.15 * flow_div_strength
    logits[MarketRegime.RANGE] += 0.10 * flow_div_strength

# Aggressive flow confirms trend
flow_delta_ratio = float(v.get("flow_delta_ratio") or 0.0)
if abs(flow_delta_ratio) > 0.3:
    if flow_delta_ratio > 0:
        logits[MarketRegime.TREND_UP] += 0.10 * min(1.0, flow_delta_ratio)
    else:
        logits[MarketRegime.TREND_DOWN] += 0.10 * min(1.0, abs(flow_delta_ratio))

# Absorption = support/resistance held
flow_absorption = float(v.get("flow_absorption_score") or 0.0)
if flow_absorption > 0.6:
    logits[MarketRegime.RANGE] += 0.12 * flow_absorption
```

### 3.5 Use order flow in scoring

**File:** `market_intelligence/scorer.py`

Add order flow as a confirmation/contradiction signal to `score()`:
```python
# Order flow confirmation
flow_delta_ratio = float(v.get("flow_delta_ratio") or 0.0)
flow_absorption = float(v.get("flow_absorption_score") or 0.0)
flow_large_bias = float(v.get("flow_large_trade_bias") or 0.0)
flow_divergence = float(v.get("flow_delta_divergence") or 0.0)
flow_div_strength = float(v.get("flow_divergence_strength") or 0.0)

flow_bonus = 0.0
# Flow confirms trend direction
if reg.regime == MarketRegime.TREND_UP and flow_delta_ratio > 0.2:
    flow_bonus += 0.06 * min(1.0, flow_delta_ratio)
    reasons.append(f"flow_confirms_up={flow_delta_ratio:.2f}")
elif reg.regime == MarketRegime.TREND_DOWN and flow_delta_ratio < -0.2:
    flow_bonus += 0.06 * min(1.0, abs(flow_delta_ratio))
    reasons.append(f"flow_confirms_down={flow_delta_ratio:.2f}")

# Large traders confirm direction
if abs(flow_large_bias) > 0.3:
    flow_bonus += 0.04 * abs(flow_large_bias)
    reasons.append(f"large_trader_bias={flow_large_bias:.2f}")

# Delta divergence = warning signal
if flow_divergence > 0.5 and flow_div_strength > 0.3:
    flow_bonus -= 0.08 * flow_div_strength
    reasons.append(f"delta_divergence={flow_div_strength:.2f}")

# Absorption at key levels
if flow_absorption > 0.6:
    flow_bonus += 0.03  # market showing strong hands
    reasons.append(f"absorption={flow_absorption:.2f}")
```

Add `flow_bonus` to `raw_score` and `"order_flow_bonus": flow_bonus` to breakdown.

---

## PHASE 4: FUNDING RATE MEAN-REVERSION MODEL

### 4.1 Funding rate statistical model

**File:** `market_intelligence/indicators.py`

Add a funding rate mean-reversion analyzer:

```python
def funding_zscore_adaptive(
    funding_history: List[float],
    short_window: int = 12,
    long_window: int = 72,
) -> Dict[str, float]:
    """Analyze funding rate with mean-reversion model.

    Instead of simple linear normalization, models funding as mean-reverting process:
    - Computes short-term vs long-term mean for regime detection
    - Identifies extreme tails (>2 sigma) as crowding signals
    - Tracks funding acceleration (is crowding getting worse?)

    Returns dict with:
        funding_deviation: how far from long-term mean (in sigma)
        funding_regime: 1.0 (crowded long), -1.0 (crowded short), 0.0 (neutral)
        funding_mean_reversion_signal: expected reversion strength [0, 1]
        funding_acceleration: rate of change of deviation
        funding_extreme: 1.0 if >2sigma, 0.0 otherwise
    """
    result = {
        "funding_deviation": 0.0,
        "funding_regime": 0.0,
        "funding_mean_reversion_signal": 0.0,
        "funding_acceleration": 0.0,
        "funding_extreme": 0.0,
    }

    if len(funding_history) < max(short_window, 6):
        return result

    # Long-term statistics
    long_data = funding_history[-long_window:] if len(funding_history) >= long_window else funding_history
    long_mean = sum(long_data) / len(long_data)
    long_var = sum((x - long_mean) ** 2 for x in long_data) / max(len(long_data) - 1, 1)
    long_std = long_var ** 0.5

    if long_std < 1e-12:
        return result

    # Short-term mean
    short_data = funding_history[-short_window:]
    short_mean = sum(short_data) / len(short_data)

    # Deviation from long-term mean in sigma
    deviation = (short_mean - long_mean) / long_std
    result["funding_deviation"] = deviation

    # Funding regime
    if deviation > 1.0:
        result["funding_regime"] = min(1.0, deviation / 3.0)
    elif deviation < -1.0:
        result["funding_regime"] = max(-1.0, deviation / 3.0)

    # Mean reversion signal: stronger when further from mean
    # Based on Ornstein-Uhlenbeck intuition: reversion force proportional to displacement
    abs_dev = abs(deviation)
    if abs_dev > 1.0:
        result["funding_mean_reversion_signal"] = min(1.0, (abs_dev - 1.0) / 2.0)

    # Extreme flag
    if abs_dev >= 2.0:
        result["funding_extreme"] = 1.0

    # Acceleration: is funding getting more extreme?
    if len(funding_history) >= short_window * 2:
        prev_short = funding_history[-(short_window * 2):-short_window]
        prev_mean = sum(prev_short) / len(prev_short)
        prev_dev = (prev_mean - long_mean) / long_std
        result["funding_acceleration"] = deviation - prev_dev

    return result
```

### 4.2 Integrate into feature engine

**File:** `market_intelligence/feature_engine.py`

Import `funding_zscore_adaptive` from indicators. After computing funding features:

```python
# Advanced funding model
funding_hist = histories.get("funding", [])
if len(funding_hist) >= 12:
    funding_model = funding_zscore_adaptive(funding_hist, short_window=12, long_window=72)
    for key, val in funding_model.items():
        values[key] = val
```

### 4.3 Use in scorer for better directional bias

**File:** `market_intelligence/scorer.py`

In the directional bias section, add funding model signals:
```python
# Advanced funding signals
funding_regime = float(v.get("funding_regime") or 0.0)
funding_mr_signal = float(v.get("funding_mean_reversion_signal") or 0.0)
funding_extreme_flag = float(v.get("funding_extreme") or 0.0)

# Extreme funding → strong mean-reversion expectation
if funding_extreme_flag > 0.5:
    if funding_regime > 0:
        # Crowded longs → expect short pressure
        reasons.append(f"funding_crowded_long={funding_regime:.2f}")
    else:
        reasons.append(f"funding_crowded_short={funding_regime:.2f}")
```

Add `"funding_mean_reversion": funding_mr_signal` to breakdown dict. Add mean_reversion_signal to directional_signals as `"funding_crowding"`.

---

## PHASE 5: LIQUIDATION CASCADE MODEL

### 5.1 Liquidation cascade detector

**File:** `market_intelligence/indicators.py`

Add:
```python
def liquidation_cascade_risk(
    liquidation_scores: List[float],
    price_changes: List[float],
    oi_deltas: List[float],
    window: int = 10,
) -> Dict[str, float]:
    """Nonlinear liquidation cascade risk model.

    Models the positive feedback loop:
    liquidation → price drop → more liquidations → flash crash

    Key insight: cascade risk is NONLINEAR — it accelerates past thresholds.

    Returns:
        cascade_risk: 0-1 probability of cascade
        cascade_stage: 0 (none), 1 (early), 2 (developing), 3 (active)
        cascade_direction: -1 (long squeeze), 1 (short squeeze), 0 (none)
    """
    result = {"cascade_risk": 0.0, "cascade_stage": 0.0, "cascade_direction": 0.0}

    if len(liquidation_scores) < 3:
        return result

    w = min(window, len(liquidation_scores))
    recent_liq = liquidation_scores[-w:]
    recent_price = price_changes[-w:] if len(price_changes) >= w else []
    recent_oi = oi_deltas[-w:] if len(oi_deltas) >= w else []

    # Average liquidation intensity
    avg_liq = sum(recent_liq) / len(recent_liq) if recent_liq else 0.0
    max_liq = max(recent_liq) if recent_liq else 0.0

    # Liquidation acceleration: are liquidations increasing?
    if len(recent_liq) >= 4:
        first_half = sum(recent_liq[:len(recent_liq)//2]) / max(len(recent_liq)//2, 1)
        second_half = sum(recent_liq[len(recent_liq)//2:]) / max(len(recent_liq) - len(recent_liq)//2, 1)
        liq_acceleration = second_half - first_half
    else:
        liq_acceleration = 0.0

    # Price-liquidation correlation (feedback loop indicator)
    price_liq_feedback = 0.0
    if recent_price and len(recent_price) == len(recent_liq):
        # Negative price change + high liquidations = cascade
        for i in range(len(recent_price)):
            if recent_price[i] < 0 and recent_liq[i] > avg_liq:
                price_liq_feedback += abs(recent_price[i]) * recent_liq[i]

    # OI contraction during liquidation = forced closure
    oi_contraction = 0.0
    if recent_oi:
        neg_oi = [d for d in recent_oi if d < 0]
        oi_contraction = abs(sum(neg_oi)) / max(len(recent_oi), 1)

    # Nonlinear cascade risk score
    # Key: risk grows QUADRATICALLY past thresholds
    base_risk = 0.0
    if max_liq > 0.5:
        base_risk += 0.3 * min(1.0, max_liq)
    if liq_acceleration > 0.2:
        base_risk += 0.3 * min(1.0, liq_acceleration ** 1.5)  # nonlinear!
    if price_liq_feedback > 0.1:
        base_risk += 0.2 * min(1.0, price_liq_feedback)
    if oi_contraction > 0:
        base_risk += 0.2 * min(1.0, oi_contraction)

    cascade_risk = min(1.0, base_risk)
    result["cascade_risk"] = cascade_risk

    # Cascade stage classification
    if cascade_risk >= 0.7:
        result["cascade_stage"] = 3.0  # Active cascade
    elif cascade_risk >= 0.4:
        result["cascade_stage"] = 2.0  # Developing
    elif cascade_risk >= 0.15:
        result["cascade_stage"] = 1.0  # Early warning
    else:
        result["cascade_stage"] = 0.0

    # Direction: negative price = long liquidation, positive = short squeeze
    if recent_price:
        avg_price_change = sum(recent_price) / len(recent_price)
        if avg_price_change < -0.001 and cascade_risk > 0.15:
            result["cascade_direction"] = -1.0  # long squeeze
        elif avg_price_change > 0.001 and cascade_risk > 0.15:
            result["cascade_direction"] = 1.0   # short squeeze

    return result
```

### 5.2 Integrate into feature engine

**File:** `market_intelligence/feature_engine.py`

```python
# Liquidation cascade model
liq_hist = histories.get("liquidation_cluster", [])
price_hist = histories.get("prices", [])
oi_hist = histories.get("open_interest", [])

if len(liq_hist) >= 3:
    price_changes = []
    if len(price_hist) >= 2:
        price_changes = [(price_hist[i] - price_hist[i-1]) / max(price_hist[i-1], 1e-12)
                         for i in range(1, len(price_hist))]
    oi_deltas_raw = []
    if len(oi_hist) >= 2:
        oi_deltas_raw = [oi_hist[i] - oi_hist[i-1] for i in range(1, len(oi_hist))]

    cascade = liquidation_cascade_risk(liq_hist, price_changes, oi_deltas_raw)
    for key, val in cascade.items():
        values[key] = val
```

### 5.3 Use in regime and scoring

**File:** `market_intelligence/regime.py`

```python
# Liquidation cascade detection
cascade_risk = float(v.get("cascade_risk") or 0.0)
cascade_stage = float(v.get("cascade_stage") or 0.0)
if cascade_stage >= 2.0:
    logits[MarketRegime.PANIC] += 0.25 * cascade_risk
    logits[MarketRegime.HIGH_VOLATILITY] += 0.15 * cascade_risk
```

**File:** `market_intelligence/scorer.py`

```python
# Cascade risk penalty
cascade_risk = float(v.get("cascade_risk") or 0.0)
cascade_stage = float(v.get("cascade_stage") or 0.0)
if cascade_stage >= 2.0:
    risk_penalty += 0.15 * cascade_risk
    reasons.append(f"cascade_risk={cascade_risk:.2f}")
```

---

## PHASE 6: MARKET MICROSTRUCTURE

### 6.1 Spread dynamics as leading indicator

**File:** `market_intelligence/indicators.py`

```python
def spread_dynamics(
    spreads_bps: List[float],
    window: int = 20,
) -> Dict[str, float]:
    """Analyze bid-ask spread dynamics as a leading indicator.

    Widening spreads precede volatility; tightening precedes calm.
    Sudden spread widening = liquidity withdrawal = danger signal.

    Returns:
        spread_regime: "tight" (< 1 sigma), "normal", "wide" (> 1 sigma), "extreme" (> 2 sigma)
        spread_expansion_rate: rate of widening (positive = widening)
        spread_percentile: current spread vs historical
        liquidity_withdrawal: 0-1 score, 1 = sudden extreme widening
    """
    result = {
        "spread_regime_code": 0.0,       # -1=tight, 0=normal, 1=wide, 2=extreme
        "spread_expansion_rate": 0.0,
        "spread_percentile": 0.5,
        "liquidity_withdrawal": 0.0,
    }

    if len(spreads_bps) < max(5, window // 2):
        return result

    data = spreads_bps[-window:]
    n = len(data)
    mean_sp = sum(data) / n
    var_sp = sum((x - mean_sp) ** 2 for x in data) / max(n - 1, 1)
    std_sp = var_sp ** 0.5

    current = data[-1]

    if std_sp < 1e-12:
        return result

    z = (current - mean_sp) / std_sp

    # Spread regime
    if z > 2.0:
        result["spread_regime_code"] = 2.0
    elif z > 1.0:
        result["spread_regime_code"] = 1.0
    elif z < -1.0:
        result["spread_regime_code"] = -1.0
    else:
        result["spread_regime_code"] = 0.0

    # Expansion rate (slope of last 5 values)
    if len(data) >= 5:
        recent = data[-5:]
        slope_num = sum((i - 2) * (recent[i] - sum(recent)/5) for i in range(5))
        slope_den = sum((i - 2) ** 2 for i in range(5))
        if slope_den > 0:
            result["spread_expansion_rate"] = slope_num / slope_den

    # Percentile
    sorted_data = sorted(data)
    rank = sum(1 for x in sorted_data if x <= current) / n
    result["spread_percentile"] = rank

    # Liquidity withdrawal: sudden jump in spread
    if n >= 3:
        prev_avg = sum(data[-4:-1]) / 3
        if prev_avg > 0:
            jump = (current - prev_avg) / prev_avg
            if jump > 0.5:  # >50% jump in spread
                result["liquidity_withdrawal"] = min(1.0, jump)

    return result
```

### 6.2 Market impact estimation

**File:** `market_intelligence/indicators.py`

```python
def estimate_market_impact(
    orderbook_bid_volume: float,
    orderbook_ask_volume: float,
    avg_trade_volume: float,
    spread_bps: float,
) -> Dict[str, float]:
    """Estimate cost of entering/exiting a position.

    Models:
    - Immediate impact (half-spread cost)
    - Depth impact (how much volume needs to be consumed)
    - Total estimated cost in bps

    Returns:
        immediate_cost_bps: half-spread
        depth_impact_bps: estimated additional slippage
        total_cost_bps: immediate + depth
        entry_feasibility: 0-1, how easily a position can be opened
    """
    immediate_cost = spread_bps / 2.0

    # Depth impact: trade size relative to available liquidity
    avg_depth = (orderbook_bid_volume + orderbook_ask_volume) / 2.0 if (orderbook_bid_volume + orderbook_ask_volume) > 0 else 1.0
    trade_to_depth = avg_trade_volume / max(avg_depth, 1e-12)

    # Square-root market impact model (standard in microstructure)
    # Impact ≈ k * sqrt(V/ADV) where V = trade size, ADV = average depth
    depth_impact = spread_bps * (trade_to_depth ** 0.5) if trade_to_depth < 10.0 else spread_bps * 3.16

    total_cost = immediate_cost + depth_impact

    # Entry feasibility: can we enter without massive slippage?
    feasibility = max(0.0, min(1.0, 1.0 - min(1.0, trade_to_depth)))

    return {
        "immediate_cost_bps": immediate_cost,
        "depth_impact_bps": depth_impact,
        "total_cost_bps": total_cost,
        "entry_feasibility": feasibility,
    }
```

### 6.3 Integrate into feature engine

**File:** `market_intelligence/feature_engine.py`

```python
# Spread dynamics
spread_hist = histories.get("spread_bps", [])
if len(spread_hist) >= 5:
    spread_dyn = spread_dynamics(spread_hist, window=20)
    for key, val in spread_dyn.items():
        values[key] = val

# Market impact estimation
ob_bid_vol = float(snapshot.orderbook_bid_volume or 0.0)
ob_ask_vol = float(snapshot.orderbook_ask_volume or 0.0)
avg_vol = float(values.get("volume_proxy") or 0.0)
sp_bps = float(values.get("spread_bps") or 0.0)
if ob_bid_vol > 0 or ob_ask_vol > 0:
    impact = estimate_market_impact(ob_bid_vol, ob_ask_vol, avg_vol, sp_bps)
    for key, val in impact.items():
        values[key] = val
```

### 6.4 Use spread dynamics in regime and scoring

**File:** `market_intelligence/regime.py`

```python
# Spread dynamics: liquidity withdrawal precedes volatility
liq_withdrawal = float(v.get("liquidity_withdrawal") or 0.0)
if liq_withdrawal > 0.5:
    logits[MarketRegime.HIGH_VOLATILITY] += 0.15 * liq_withdrawal
    logits[MarketRegime.RANGE] -= 0.10 * liq_withdrawal
```

**File:** `market_intelligence/scorer.py`

```python
# Market microstructure penalties
total_cost_bps = float(v.get("total_cost_bps") or 0.0)
entry_feasibility = float(v.get("entry_feasibility") or 1.0)
liq_withdrawal = float(v.get("liquidity_withdrawal") or 0.0)

# Penalize high-cost entries
if total_cost_bps > 10.0:
    micro_penalty = min(0.10, (total_cost_bps - 10.0) / 100.0)
    risk_penalty += micro_penalty
    reasons.append(f"high_entry_cost={total_cost_bps:.1f}bps")

# Penalize low feasibility
if entry_feasibility < 0.5:
    risk_penalty += 0.05 * (1.0 - entry_feasibility)
    reasons.append(f"low_feasibility={entry_feasibility:.2f}")

# Liquidity withdrawal warning
if liq_withdrawal > 0.5:
    risk_penalty += 0.08 * liq_withdrawal
    reasons.append(f"liquidity_withdrawal={liq_withdrawal:.2f}")
```

---

## PHASE 7: ADVANCED ML OPTIMIZER

### 7.1 Exponentially weighted ridge regression

**File:** `market_intelligence/ml_weights.py`

The current optimizer uses a flat deque of 5000 records without decay. Replace with exponentially weighted approach.

Find the `_recompute()` method and modify it:

```python
def _recompute(self) -> Optional[Dict[str, float]]:
    """Recompute weights using exponentially weighted ridge regression.

    Key improvement: recent observations have exponentially more weight
    than old ones, so the model adapts to regime changes.
    """
    if len(self._records) < self._min_records:
        return None

    records = list(self._records)
    n = len(records)

    # Exponential decay weights: most recent = 1.0, oldest ≈ decay^n
    decay = 0.995  # half-life ≈ 138 records
    time_weights = [decay ** (n - 1 - i) for i in range(n)]

    # ... rest of ridge regression with weighted samples
    # When building X^T W X + lambda*I, multiply each row by sqrt(time_weight)
```

Add this concrete implementation. In the matrix construction:
```python
# Apply time weights to samples
for i in range(n):
    w = time_weights[i] ** 0.5  # sqrt for weighted least squares
    for j in range(p):
        X[i][j] *= w
    y[i] *= w
```

This ensures old observations naturally fade out without explicit purging.

### 7.2 Add outcome tracking for feedback quality

Add a method to track prediction quality:
```python
def get_prediction_accuracy(self, last_n: int = 100) -> Dict[str, float]:
    """Return accuracy metrics for recent predictions."""
    if len(self._records) < 10:
        return {"accuracy": 0.0, "sample_size": 0}

    records = list(self._records)[-last_n:]
    correct = 0
    total = 0
    for features, predicted_score, actual_outcome in records:
        if actual_outcome is not None:
            total += 1
            # Did we predict the right direction?
            if (predicted_score > 50 and actual_outcome > 0) or \
               (predicted_score <= 50 and actual_outcome <= 0):
                correct += 1

    accuracy = correct / max(total, 1)
    return {
        "accuracy": accuracy,
        "sample_size": total,
        "effective_weight_sum": sum(0.995 ** (len(records) - 1 - i) for i in range(len(records))),
    }
```

---

## PHASE 8: BACKTESTING FRAMEWORK

### 8.1 JSONL replay engine

**New file:** `market_intelligence/backtest.py`

```python
"""Backtesting framework for Market Intelligence System.

Replays JSONL log files and evaluates signal quality ex-post.
No external dependencies.

Usage:
    from market_intelligence.backtest import BacktestRunner
    runner = BacktestRunner("logs/market_intelligence.jsonl")
    results = runner.run()
    print(results.summary())
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SignalOutcome:
    """Tracks what happened after a signal was generated."""
    timestamp: float
    symbol: str
    score: float
    directional_bias: str
    regime: str
    confidence: float
    # Outcome (filled during replay)
    price_at_signal: float = 0.0
    price_after_1h: float = 0.0
    price_after_4h: float = 0.0
    price_after_24h: float = 0.0
    return_1h_pct: float = 0.0
    return_4h_pct: float = 0.0
    return_24h_pct: float = 0.0
    was_profitable_1h: bool = False
    was_profitable_4h: bool = False


@dataclass
class RegimeTransition:
    """Records when regime changed and what happened after."""
    timestamp: float
    from_regime: str
    to_regime: str
    confidence: float
    was_correct: bool = False  # Did market behavior match new regime?


@dataclass
class BacktestResults:
    """Complete backtest results."""
    total_signals: int = 0
    signals_with_outcome: int = 0

    # Directional accuracy
    long_signals: int = 0
    long_correct_1h: int = 0
    long_correct_4h: int = 0
    short_signals: int = 0
    short_correct_1h: int = 0
    short_correct_4h: int = 0

    # Score distribution
    avg_score_profitable: float = 0.0
    avg_score_unprofitable: float = 0.0

    # Regime accuracy
    regime_transitions: int = 0
    regime_correct: int = 0

    # Risk metrics
    max_drawdown_following_signal: float = 0.0
    sharpe_proxy: float = 0.0  # avg return / std return for signals

    signal_outcomes: List[SignalOutcome] = field(default_factory=list)
    regime_transitions_log: List[RegimeTransition] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=== BACKTEST RESULTS ===",
            f"Total signals: {self.total_signals}",
            f"Signals with outcome: {self.signals_with_outcome}",
            "",
            "--- Directional Accuracy ---",
            f"Long signals: {self.long_signals}",
            f"  Correct @1h: {self.long_correct_1h} ({self.long_correct_1h/max(self.long_signals,1)*100:.1f}%)",
            f"  Correct @4h: {self.long_correct_4h} ({self.long_correct_4h/max(self.long_signals,1)*100:.1f}%)",
            f"Short signals: {self.short_signals}",
            f"  Correct @1h: {self.short_correct_1h} ({self.short_correct_1h/max(self.short_signals,1)*100:.1f}%)",
            f"  Correct @4h: {self.short_correct_4h} ({self.short_correct_4h/max(self.short_signals,1)*100:.1f}%)",
            "",
            "--- Score Quality ---",
            f"Avg score (profitable): {self.avg_score_profitable:.1f}",
            f"Avg score (unprofitable): {self.avg_score_unprofitable:.1f}",
            f"Score separation: {self.avg_score_profitable - self.avg_score_unprofitable:.1f}",
            "",
            "--- Regime Model ---",
            f"Regime transitions: {self.regime_transitions}",
            f"Correct transitions: {self.regime_correct} ({self.regime_correct/max(self.regime_transitions,1)*100:.1f}%)",
            "",
            "--- Risk ---",
            f"Max drawdown after signal: {self.max_drawdown_following_signal:.2f}%",
            f"Sharpe proxy: {self.sharpe_proxy:.2f}",
        ]
        return "\n".join(lines)


class BacktestRunner:
    """Replays JSONL logs and evaluates signal quality."""

    def __init__(self, jsonl_path: str, min_score: float = 30.0):
        self._path = jsonl_path
        self._min_score = min_score

    def run(self) -> BacktestResults:
        """Run backtest over entire log file."""
        if not os.path.exists(self._path):
            raise FileNotFoundError(f"Log file not found: {self._path}")

        # Load all records
        records: List[Dict[str, Any]] = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not records:
            return BacktestResults()

        # Build price timeline per symbol
        price_timeline: Dict[str, List[Tuple[float, float]]] = {}
        for rec in records:
            ts = rec.get("timestamp", 0.0)
            for sym, data in rec.get("features", {}).items():
                price = data.get("price") or data.get("close")
                if price:
                    if sym not in price_timeline:
                        price_timeline[sym] = []
                    price_timeline[sym].append((ts, float(price)))

        # Sort timelines
        for sym in price_timeline:
            price_timeline[sym].sort(key=lambda x: x[0])

        results = BacktestResults()

        # Evaluate signals
        prev_regime = None
        for rec in records:
            ts = rec.get("timestamp", 0.0)

            # Track regime transitions
            global_regime = rec.get("global_regime", {})
            current_regime = global_regime.get("regime", "")
            if prev_regime and current_regime != prev_regime:
                rt = RegimeTransition(
                    timestamp=ts,
                    from_regime=prev_regime,
                    to_regime=current_regime,
                    confidence=global_regime.get("confidence", 0.0),
                )
                results.regime_transitions += 1
                results.regime_transitions_log.append(rt)
            prev_regime = current_regime

            # Evaluate opportunity signals
            for opp in rec.get("opportunities", []):
                symbol = opp.get("symbol", "")
                score = float(opp.get("score", 0.0))
                bias = opp.get("directional_bias", "neutral")
                confidence = float(opp.get("confidence", 0.0))

                if score < self._min_score or bias == "neutral":
                    continue

                results.total_signals += 1

                # Find price at signal and future prices
                timeline = price_timeline.get(symbol, [])
                price_at = self._find_price(timeline, ts)
                price_1h = self._find_price(timeline, ts + 3600)
                price_4h = self._find_price(timeline, ts + 14400)
                price_24h = self._find_price(timeline, ts + 86400)

                if price_at is None or price_1h is None:
                    continue

                results.signals_with_outcome += 1

                ret_1h = (price_1h - price_at) / price_at * 100 if price_at > 0 else 0.0
                ret_4h = ((price_4h - price_at) / price_at * 100) if price_4h and price_at > 0 else 0.0
                ret_24h = ((price_24h - price_at) / price_at * 100) if price_24h and price_at > 0 else 0.0

                outcome = SignalOutcome(
                    timestamp=ts, symbol=symbol, score=score,
                    directional_bias=bias, regime=current_regime, confidence=confidence,
                    price_at_signal=price_at, price_after_1h=price_1h,
                    price_after_4h=price_4h or 0.0, price_after_24h=price_24h or 0.0,
                    return_1h_pct=ret_1h, return_4h_pct=ret_4h, return_24h_pct=ret_24h,
                )

                # Evaluate correctness
                if bias == "long":
                    results.long_signals += 1
                    outcome.was_profitable_1h = ret_1h > 0
                    outcome.was_profitable_4h = ret_4h > 0
                    if ret_1h > 0:
                        results.long_correct_1h += 1
                    if ret_4h > 0:
                        results.long_correct_4h += 1
                elif bias == "short":
                    results.short_signals += 1
                    outcome.was_profitable_1h = ret_1h < 0
                    outcome.was_profitable_4h = ret_4h < 0
                    if ret_1h < 0:
                        results.short_correct_1h += 1
                    if ret_4h < 0:
                        results.short_correct_4h += 1

                results.signal_outcomes.append(outcome)

        # Compute summary statistics
        if results.signal_outcomes:
            profitable = [o for o in results.signal_outcomes if o.was_profitable_4h]
            unprofitable = [o for o in results.signal_outcomes if not o.was_profitable_4h]
            if profitable:
                results.avg_score_profitable = sum(o.score for o in profitable) / len(profitable)
            if unprofitable:
                results.avg_score_unprofitable = sum(o.score for o in unprofitable) / len(unprofitable)

            # Sharpe proxy
            returns = [o.return_4h_pct for o in results.signal_outcomes if o.return_4h_pct != 0]
            if len(returns) >= 2:
                mean_ret = sum(returns) / len(returns)
                std_ret = (sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5
                results.sharpe_proxy = mean_ret / max(std_ret, 1e-12)

            # Max drawdown
            max_dd = max((abs(o.return_4h_pct) for o in results.signal_outcomes if not o.was_profitable_4h), default=0.0)
            results.max_drawdown_following_signal = max_dd

        return results

    @staticmethod
    def _find_price(timeline: List[Tuple[float, float]], target_ts: float) -> Optional[float]:
        """Find the closest price to target timestamp (within 10 minutes tolerance)."""
        if not timeline:
            return None
        best = None
        best_diff = float("inf")
        for ts, price in timeline:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = price
        if best_diff > 600:  # 10 min tolerance
            return None
        return best
```

### 8.2 Add backtest command to integration

**File:** `market_intelligence/integration.py`

```python
async def run_backtest(jsonl_path: Optional[str] = None, min_score: float = 30.0) -> str:
    """Run backtest on collected data and return summary."""
    from market_intelligence.backtest import BacktestRunner
    from market_intelligence.config import MIConfig

    if jsonl_path is None:
        cfg = MIConfig.from_env()
        jsonl_path = os.path.join(cfg.log_dir, cfg.jsonl_file)

    runner = BacktestRunner(jsonl_path, min_score=min_score)
    results = runner.run()
    return results.summary()
```

---

## PHASE 9: CONCURRENCY FIX

### 9.1 Use ProcessPoolExecutor for CPU-bound work

**File:** `market_intelligence/engine.py`

The current code runs `_compute_pipeline()` in a thread via `asyncio.to_thread()`. Due to Python's GIL, this provides no benefit for CPU-bound work.

Replace with `ProcessPoolExecutor` for the feature computation:

```python
import concurrent.futures

# At module level or in __init__
_process_pool: Optional[concurrent.futures.ProcessPoolExecutor] = None

def _get_process_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _process_pool
    if _process_pool is None:
        _process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    return _process_pool
```

**IMPORTANT CAVEAT**: ProcessPoolExecutor requires picklable arguments. If the current `_compute_pipeline` method uses `self` extensively, it may not be easy to pickle. In that case, keep `asyncio.to_thread()` but add a comment explaining why:

```python
# NOTE: Using thread pool instead of process pool because _compute_pipeline
# accesses shared state (self._feature_engine, self._regime_model) that
# isn't easily picklable. The GIL limitation is acceptable here because
# the actual CPU-bound work (math operations) releases the GIL in C extensions.
# If profiling shows this is a bottleneck, extract pure computation into
# a standalone function with serializable inputs/outputs.
```

Actually, check the code: if `_compute_pipeline` can be refactored to accept and return plain dicts (no self references), use ProcessPoolExecutor. Otherwise, keep threads with the explanatory comment and ensure the main event loop isn't blocked.

---

## PHASE 10: PERSISTENCE VERSIONING

### 10.1 Add schema version to persistence

**File:** `market_intelligence/persistence.py`

Add a version constant and migration logic:

```python
SCHEMA_VERSION = 2  # Increment when state structure changes

def save_state(path: str, collector, regime_model, feature_engine, order_flow=None) -> None:
    """Save state with schema version for forward compatibility."""
    state = {
        "_schema_version": SCHEMA_VERSION,
        "_saved_at": time.time(),
        "collector": _serialize_collector(collector),
        "regime": regime_model.to_dict() if regime_model else {},
        "features": feature_engine.to_dict() if feature_engine else {},
    }
    if order_flow is not None:
        state["order_flow"] = order_flow.to_dict()
    # ... atomic write ...


def load_state(path: str) -> Optional[Dict[str, Any]]:
    """Load state with version checking and migration."""
    # ... existing load logic ...
    if data is None:
        return None

    version = data.get("_schema_version", 1)
    if version > SCHEMA_VERSION:
        logger.warning("State file version %d is newer than code version %d. Ignoring.", version, SCHEMA_VERSION)
        return None

    if version < SCHEMA_VERSION:
        data = _migrate_state(data, version, SCHEMA_VERSION)

    return data


def _migrate_state(data: Dict[str, Any], from_version: int, to_version: int) -> Dict[str, Any]:
    """Migrate state from older schema versions."""
    if from_version < 2:
        # v1 → v2: add order_flow key
        if "order_flow" not in data:
            data["order_flow"] = {}
        data["_schema_version"] = 2

    return data
```

---

## PHASE 11: OUTPUT ENHANCEMENTS

### 11.1 Add order flow to human report

**File:** `market_intelligence/output.py`

In `format_human_report()`, after trading conditions section:

```python
# Order flow summary (BTC)
btc_features = payload.get("features", {}).get(btc_sym, {})
flow_delta = btc_features.get("flow_delta_ratio")
flow_div = btc_features.get("flow_delta_divergence")
flow_absorption = btc_features.get("flow_absorption_score")
cascade_risk = btc_features.get("cascade_risk")

flow_parts = []
if flow_delta is not None:
    fd = float(flow_delta)
    if fd > 0.2:
        flow_parts.append(f"покупатели доминируют ({fd:+.2f})")
    elif fd < -0.2:
        flow_parts.append(f"продавцы доминируют ({fd:+.2f})")
    else:
        flow_parts.append("баланс сил")

if flow_div and float(flow_div) > 0.5:
    flow_parts.append("⚠ дивергенция дельты")

if flow_absorption and float(flow_absorption) > 0.5:
    flow_parts.append("обнаружена абсорбция")

if cascade_risk and float(cascade_risk) > 0.3:
    cr = float(cascade_risk)
    stage = int(float(btc_features.get("cascade_stage", 0)))
    stage_names = {0: "", 1: "ранняя", 2: "развивающаяся", 3: "активная"}
    flow_parts.append(f"⚠ каскад ликвидаций: {stage_names.get(stage, '')} ({cr:.0%})")

if flow_parts:
    lines.append("\n📊 Поток ордеров: " + " | ".join(flow_parts))
```

### 11.2 Add funding model to report

```python
# Funding model
funding_dev = btc_features.get("funding_deviation")
funding_mr = btc_features.get("funding_mean_reversion_signal")
if funding_dev is not None:
    fd = float(funding_dev)
    if abs(fd) > 1.5:
        direction = "лонги перегружены" if fd > 0 else "шорты перегружены"
        lines.append(f"💰 Фандинг: {direction} ({fd:+.1f}σ)")
        if funding_mr and float(funding_mr) > 0.3:
            lines.append(f"   Сигнал возврата к среднему: {float(funding_mr):.0%}")
```

### 11.3 Add microstructure to report

```python
# Microstructure
spread_regime = btc_features.get("spread_regime_code")
liq_withdrawal = btc_features.get("liquidity_withdrawal")
total_cost = btc_features.get("total_cost_bps")

if liq_withdrawal and float(liq_withdrawal) > 0.5:
    lines.append(f"⚠ Отток ликвидности: {float(liq_withdrawal):.0%}")
if total_cost and float(total_cost) > 15:
    lines.append(f"⚠ Высокая стоимость входа: {float(total_cost):.1f} bps")
```

---

## PHASE 12: COMPREHENSIVE TESTS

**File:** `tests/test_market_intelligence.py`

Add ALL of the following tests AT THE END of the file (do NOT modify existing tests):

```python
# ============================================================
# PHASE 12: Comprehensive test suite for 10/10 quality
# ============================================================

import time
import math


# --- Order Flow Tests ---

def test_order_flow_delta_profile_basic():
    """Delta profile correctly separates buy/sell volume."""
    from market_intelligence.order_flow import OrderFlowAnalyzer
    analyzer = OrderFlowAnalyzer()
    now = time.time()
    trades = [
        (now - 10, 100.0, 5.0, "buy", False),
        (now - 8, 100.1, 3.0, "buy", False),
        (now - 5, 99.9, 10.0, "sell", False),
        (now - 2, 99.8, 2.0, "sell", True),
    ]
    analyzer.push_trades("TEST", trades)
    profile = analyzer.compute_delta_profile("TEST", window_seconds=60)

    assert profile.total_buy_volume == 8.0
    assert profile.total_sell_volume == 12.0
    assert profile.net_delta == -4.0
    assert profile.delta_ratio < 0  # sellers dominate


def test_order_flow_delta_divergence():
    """Delta divergence detected when price up but delta negative."""
    from market_intelligence.order_flow import OrderFlowAnalyzer
    analyzer = OrderFlowAnalyzer()
    now = time.time()
    # Price goes up but more sell volume
    trades = [
        (now - 20, 100.0, 1.0, "buy", False),
        (now - 15, 100.5, 1.0, "buy", False),
        (now - 10, 101.0, 5.0, "sell", False),
        (now - 5, 101.5, 4.0, "sell", False),
        (now - 1, 102.0, 1.0, "buy", False),
    ]
    analyzer.push_trades("TEST", trades)
    profile = analyzer.compute_delta_profile("TEST", window_seconds=60)

    # Price went from 100 to 102 (up), but net delta = 3 buy - 9 sell = -6 (negative)
    assert profile.delta_divergence is True
    assert profile.divergence_strength > 0.0


def test_order_flow_empty_state():
    """Empty analyzer returns zeroed profile."""
    from market_intelligence.order_flow import OrderFlowAnalyzer
    analyzer = OrderFlowAnalyzer()
    profile = analyzer.compute_delta_profile("UNKNOWN", window_seconds=60)
    assert profile.net_delta == 0.0
    assert profile.delta_ratio == 0.0


def test_order_flow_features_dict():
    """get_flow_features returns all expected keys."""
    from market_intelligence.order_flow import OrderFlowAnalyzer
    analyzer = OrderFlowAnalyzer()
    now = time.time()
    trades = [(now - i, 100.0, 1.0, "buy" if i % 2 == 0 else "sell", False) for i in range(20)]
    analyzer.push_trades("TEST", trades)
    features = analyzer.get_flow_features("TEST")

    expected_keys = [
        "flow_net_delta", "flow_delta_ratio", "flow_aggressive_buy_pct",
        "flow_aggressive_sell_pct", "flow_absorption_score", "flow_large_trade_bias",
        "flow_delta_divergence", "flow_divergence_strength", "flow_delta_momentum",
    ]
    for key in expected_keys:
        assert key in features, f"Missing key: {key}"


def test_order_flow_persistence():
    """Order flow state can be serialized and restored."""
    from market_intelligence.order_flow import OrderFlowAnalyzer
    analyzer = OrderFlowAnalyzer()
    now = time.time()
    trades = [(now - i, 100.0 + i * 0.1, 1.0, "buy", False) for i in range(10)]
    analyzer.push_trades("TEST", trades)
    analyzer.compute_delta_profile("TEST")

    data = analyzer.to_dict()
    assert "TEST" in data

    analyzer2 = OrderFlowAnalyzer()
    analyzer2.restore_from_dict(data)
    state = analyzer2._get_state("TEST")
    assert len(state.delta_history) > 0


# --- Funding Mean-Reversion Tests ---

def test_funding_zscore_adaptive_neutral():
    """Stable funding returns neutral signals."""
    from market_intelligence.indicators import funding_zscore_adaptive
    history = [0.0001] * 100  # stable funding
    result = funding_zscore_adaptive(history)
    assert abs(result["funding_deviation"]) < 0.5
    assert result["funding_extreme"] == 0.0


def test_funding_zscore_adaptive_extreme():
    """Extreme funding detected correctly."""
    from market_intelligence.indicators import funding_zscore_adaptive
    # Normal history then extreme spike
    history = [0.0001] * 60 + [0.005] * 12  # massive funding spike
    result = funding_zscore_adaptive(history)
    assert result["funding_deviation"] > 1.5
    assert result["funding_extreme"] == 1.0
    assert result["funding_mean_reversion_signal"] > 0.0


def test_funding_zscore_insufficient_data():
    """Insufficient data returns zeros."""
    from market_intelligence.indicators import funding_zscore_adaptive
    result = funding_zscore_adaptive([0.001, 0.002])
    assert result["funding_deviation"] == 0.0
    assert result["funding_extreme"] == 0.0


# --- Liquidation Cascade Tests ---

def test_liquidation_cascade_no_risk():
    """Low liquidation activity returns zero risk."""
    from market_intelligence.indicators import liquidation_cascade_risk
    result = liquidation_cascade_risk(
        [0.1, 0.1, 0.1, 0.1, 0.1],
        [-0.001, 0.001, -0.001, 0.001],
        [10, -5, 10, -5],
    )
    assert result["cascade_risk"] < 0.15
    assert result["cascade_stage"] == 0.0


def test_liquidation_cascade_active():
    """High and accelerating liquidations detected as cascade."""
    from market_intelligence.indicators import liquidation_cascade_risk
    # Escalating liquidation scores
    liq = [0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5]
    price_changes = [-0.01, -0.02, -0.03, -0.04, -0.05, -0.06, -0.07, -0.08, -0.09]
    oi_deltas = [-100, -200, -300, -500, -800, -1000, -1500, -2000, -2500]
    result = liquidation_cascade_risk(liq, price_changes, oi_deltas)
    assert result["cascade_risk"] > 0.4
    assert result["cascade_stage"] >= 2.0
    assert result["cascade_direction"] == -1.0  # long squeeze


def test_liquidation_cascade_insufficient_data():
    """Insufficient data returns zero."""
    from market_intelligence.indicators import liquidation_cascade_risk
    result = liquidation_cascade_risk([0.5, 0.6], [0.01], [10])
    assert result["cascade_risk"] == 0.0


# --- Spread Dynamics Tests ---

def test_spread_dynamics_normal():
    """Normal spread returns neutral regime."""
    from market_intelligence.indicators import spread_dynamics
    spreads = [5.0 + (i % 3) * 0.5 for i in range(20)]
    result = spread_dynamics(spreads)
    assert -1.5 <= result["spread_regime_code"] <= 1.5
    assert 0.0 <= result["spread_percentile"] <= 1.0


def test_spread_dynamics_extreme_widening():
    """Sudden spread widening detected as liquidity withdrawal."""
    from market_intelligence.indicators import spread_dynamics
    spreads = [5.0] * 17 + [5.0, 5.0, 25.0]  # sudden 5x jump
    result = spread_dynamics(spreads)
    assert result["spread_regime_code"] >= 1.0  # wide or extreme
    assert result["liquidity_withdrawal"] > 0.3


def test_spread_dynamics_insufficient():
    """Insufficient data returns defaults."""
    from market_intelligence.indicators import spread_dynamics
    result = spread_dynamics([5.0, 6.0])
    assert result["spread_regime_code"] == 0.0


# --- Market Impact Tests ---

def test_market_impact_low_liquidity():
    """Low liquidity results in high impact cost."""
    from market_intelligence.indicators import estimate_market_impact
    result = estimate_market_impact(
        orderbook_bid_volume=10.0,
        orderbook_ask_volume=10.0,
        avg_trade_volume=50.0,  # trade > depth = high impact
        spread_bps=10.0,
    )
    assert result["total_cost_bps"] > result["immediate_cost_bps"]
    assert result["entry_feasibility"] < 0.5


def test_market_impact_deep_book():
    """Deep orderbook results in low impact."""
    from market_intelligence.indicators import estimate_market_impact
    result = estimate_market_impact(
        orderbook_bid_volume=10000.0,
        orderbook_ask_volume=10000.0,
        avg_trade_volume=10.0,  # tiny trade vs huge depth
        spread_bps=2.0,
    )
    assert result["total_cost_bps"] < 5.0
    assert result["entry_feasibility"] > 0.9


# --- Metrics Tests ---

def test_metrics_latency_tracking():
    """Metrics correctly tracks latency stats."""
    from market_intelligence.metrics import InProcessMetrics
    m = InProcessMetrics()
    for i in range(10):
        m.record_latency("test_stage", float(i * 10))  # 0, 10, 20, ..., 90
    stats = m.get_latency_stats("test_stage")
    assert stats["count"] == 10
    assert stats["min_ms"] == 0.0
    assert stats["max_ms"] == 90.0
    assert 40.0 <= stats["avg_ms"] <= 50.0  # should be 45


def test_metrics_exchange_health():
    """Exchange health tracks success/failure rates."""
    from market_intelligence.metrics import InProcessMetrics
    m = InProcessMetrics()
    for _ in range(7):
        m.record_exchange_call("okx", True)
    for _ in range(3):
        m.record_exchange_call("okx", False)
    health = m.get_exchange_health()
    assert health["okx"]["total_calls"] == 10
    assert health["okx"]["errors"] == 3
    assert health["okx"]["error_rate"] == 0.3
    assert health["okx"]["healthy"] is True  # 30% threshold


def test_metrics_snapshot_structure():
    """Metrics snapshot contains expected keys."""
    from market_intelligence.metrics import InProcessMetrics
    m = InProcessMetrics()
    m.record_latency("total", 100.0)
    m.record_counter("cycles")
    m.record_gauge("confidence", 0.85)
    m.record_event("regime_change", {"to": "PANIC"})
    snap = m.get_snapshot()
    assert "latencies" in snap
    assert "counters" in snap
    assert "gauges" in snap
    assert "recent_events" in snap


# --- Backtest Framework Tests ---

def test_backtest_runner_missing_file():
    """Backtest raises on missing file."""
    from market_intelligence.backtest import BacktestRunner
    import pytest
    runner = BacktestRunner("/nonexistent/path.jsonl")
    with pytest.raises(FileNotFoundError):
        runner.run()


def test_backtest_runner_empty_file(tmp_path):
    """Backtest handles empty file gracefully."""
    from market_intelligence.backtest import BacktestRunner
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("")
    runner = BacktestRunner(str(empty_file))
    results = runner.run()
    assert results.total_signals == 0


def test_backtest_runner_with_data(tmp_path):
    """Backtest processes real-looking records."""
    from market_intelligence.backtest import BacktestRunner
    import json

    log_file = tmp_path / "test.jsonl"
    now = time.time()
    records = []
    for i in range(20):
        ts = now + i * 300  # 5min intervals
        price = 50000 + i * 100  # uptrend
        records.append(json.dumps({
            "timestamp": ts,
            "global_regime": {"regime": "trend_up", "confidence": 0.7},
            "features": {
                "BTCUSDT": {"price": price},
            },
            "opportunities": [
                {
                    "symbol": "BTCUSDT",
                    "score": 65.0,
                    "directional_bias": "long",
                    "confidence": 0.7,
                }
            ] if i == 5 else [],
        }))

    log_file.write_text("\n".join(records))
    runner = BacktestRunner(str(log_file), min_score=30.0)
    results = runner.run()
    assert results.total_signals >= 1
    summary = results.summary()
    assert "BACKTEST RESULTS" in summary


# --- Persistence Versioning Tests ---

def test_persistence_schema_version():
    """Persistence includes schema version."""
    from market_intelligence.persistence import SCHEMA_VERSION
    assert SCHEMA_VERSION >= 2


# --- Protocol Tests ---

def test_orderbook_depth_metrics_dataclass():
    """OrderbookDepthMetrics correctly stores values."""
    from market_intelligence.protocols import OrderbookDepthMetrics
    metrics = OrderbookDepthMetrics(
        bid_volume_total=1000.0,
        ask_volume_total=800.0,
        imbalance=0.111,
        bid_depth_weighted_price=50000.0,
        ask_depth_weighted_price=50100.0,
        spread_bps=2.0,
        concentration_top3_bid=0.6,
        concentration_top3_ask=0.55,
        levels_count=20,
    )
    assert metrics.imbalance == 0.111
    assert metrics.levels_count == 20


def test_normalized_orderbook_frozen():
    """NormalizedOrderbook is frozen (immutable)."""
    from market_intelligence.protocols import NormalizedOrderbook
    ob = NormalizedOrderbook(
        bids=[[50000, 1.0]], asks=[[50100, 1.0]],
        timestamp=time.time(), exchange="okx", symbol="BTCUSDT",
    )
    try:
        ob.exchange = "htx"  # type: ignore
        assert False, "Should be frozen"
    except (AttributeError, TypeError):
        pass  # Expected — frozen dataclass


# --- Regime Boundary Tests ---

def test_regime_rsi_boundary_68():
    """RSI exactly at 68 should NOT trigger overheated (threshold is >68)."""
    # This is a boundary test — construct features at the edge
    from market_intelligence.regime import RegimeModel
    from market_intelligence.models import FeatureVector, MarketRegime
    model = RegimeModel(confidence_threshold=0.55, min_duration_cycles=1, smoothing_alpha=0.1)

    vals = {
        "ema_cross": 0.5, "adx": 25.0, "rsi": 68.0,
        "funding_rate": 0.0, "liquidation_cluster": 0.0,
        "rolling_volatility": 0.01, "bb_width": 0.02,
        "volume_spike": 1.0, "volume_trend": 1.0,
        "market_structure_code": 0.0, "atr_percentile": 0.5,
    }
    fv = FeatureVector("BTCUSDT", time.time(), vals, vals)
    result = model.classify_global(fv)
    # At RSI=68, the overheated logit should be minimal
    # The regime should NOT be OVERHEATED with high confidence
    if result.regime == MarketRegime.OVERHEATED:
        assert result.confidence < 0.6, "RSI=68 should not give high OVERHEATED confidence"


def test_regime_rsi_boundary_85_extreme():
    """RSI > 85 should trigger extreme fast-path."""
    from market_intelligence.regime import RegimeModel
    from market_intelligence.models import FeatureVector
    model = RegimeModel(confidence_threshold=0.55, min_duration_cycles=5, smoothing_alpha=0.35)

    vals = {
        "ema_cross": 2.0, "adx": 40.0, "rsi": 87.0,
        "funding_rate": 0.005, "liquidation_cluster": 0.0,
        "rolling_volatility": 0.03, "bb_width": 0.05,
        "volume_spike": 3.0, "volume_trend": 2.0,
        "market_structure_code": 1.0, "atr_percentile": 0.8,
    }
    fv = FeatureVector("BTCUSDT", time.time(), vals, vals)
    assert model._is_extreme(fv) is True


# --- Scoring Monotonicity Tests ---

def test_scorer_higher_volatility_higher_score():
    """Higher volatility expansion should generally increase score (monotonicity)."""
    from market_intelligence.scorer import OpportunityScorer
    from market_intelligence.models import FeatureVector, RegimeState, MarketRegime
    scorer = OpportunityScorer()

    base = {
        "funding_rate": 0.0003, "funding_delta": 0.0001,
        "oi_delta": 100.0, "oi_delta_pct": 5.0,
        "rolling_volatility": 0.005, "volume_proxy": 1000.0,
        "basis_bps": 10.0, "funding_pct": 0.03,
        "macd_hist": 0.01, "volume_spike": 1.2, "cvd": 0.1,
        "data_quality_code": 0.0, "basis_acceleration": 0.0,
        "spread_bps": 5.0,
    }
    base_z = {
        "funding_rate": 0.4, "funding_delta": 0.2,
        "oi_delta": 0.6, "rolling_volatility": 0.3,
    }

    reg = RegimeState(MarketRegime.TREND_UP, 0.7, {MarketRegime.TREND_UP: 0.7}, 5)

    # Low volatility
    low_vol = dict(base, rolling_volatility_local=0.005, bb_width_local=0.01)
    low_vol_z = dict(base_z, rolling_volatility_local=0.2, bb_width_local=0.2)
    fv_low = FeatureVector("BTCUSDT", time.time(), low_vol, low_vol_z)

    # High volatility
    high_vol = dict(base, rolling_volatility_local=0.05, bb_width_local=0.08)
    high_vol_z = dict(base_z, rolling_volatility_local=2.0, bb_width_local=2.0)
    fv_high = FeatureVector("BTCUSDT", time.time(), high_vol, high_vol_z)

    scores_low = scorer.score(
        {"BTCUSDT": fv_low}, {"BTCUSDT": reg}, {"BTCUSDT": 0.5}, {"BTCUSDT": 500},
    )
    scores_high = scorer.score(
        {"BTCUSDT": fv_high}, {"BTCUSDT": reg}, {"BTCUSDT": 0.5}, {"BTCUSDT": 500},
    )

    # Higher volatility expansion should give higher or equal score
    assert scores_high[0].score >= scores_low[0].score - 5.0, \
        f"High vol score ({scores_high[0].score}) should be >= low vol ({scores_low[0].score}) minus tolerance"
```

---

## PHASE 13: FINAL VALIDATION

After ALL changes, run:

```bash
python -m pytest tests/test_market_intelligence.py -v
```

All tests (old AND new) must pass. If any test fails:
1. Read the failing test
2. Determine if the test expectation is wrong or your code is wrong
3. Fix the actual bug, do not blindly adjust test expectations
4. Re-run tests

Then verify code consistency:
- Every new field added to `values` dict in `feature_engine.py` must be handled gracefully (None fallbacks)
- Every new indicator used in `regime.py` must handle `None` with `or 0.0` fallback
- Every new `reasons.append()` in `scorer.py` must correspond to a real computed value
- New modules (`order_flow.py`, `metrics.py`, `backtest.py`, `protocols.py`) must be importable standalone
- `_assert_consistency` in `engine.py` should not need changes (new fields are optional)
- Persistence versioning must not break loading of old state files (migration handles it)

---

## SUMMARY OF ALL CHANGES

### New Files (4):
1. `market_intelligence/protocols.py` — ABC/Protocol for exchange adapters, scoring strategies, metrics
2. `market_intelligence/metrics.py` — In-process observability engine (latency, counters, gauges, events)
3. `market_intelligence/order_flow.py` — Order flow engine (delta profiles, absorption, divergence, large trades)
4. `market_intelligence/backtest.py` — JSONL replay backtesting framework with signal quality evaluation

### Modified Files (9):
1. `market_intelligence/indicators.py` — Add `funding_zscore_adaptive()`, `liquidation_cascade_risk()`, `spread_dynamics()`, `estimate_market_impact()`
2. `market_intelligence/collector.py` — Add `OrderFlowAnalyzer` integration, `_update_order_flow()` method
3. `market_intelligence/feature_engine.py` — Integrate order flow, funding model, cascade model, spread dynamics, market impact
4. `market_intelligence/regime.py` — Add order flow signals, cascade risk, spread dynamics to classification
5. `market_intelligence/scorer.py` — Add order flow, microstructure, cascade risk to scoring and bias
6. `market_intelligence/engine.py` — Add metrics instrumentation, concurrency comment/fix
7. `market_intelligence/output.py` — Add order flow, funding model, microstructure, metrics to reports
8. `market_intelligence/persistence.py` — Add schema versioning and migration
9. `market_intelligence/service.py` — Add metrics to health check
10. `market_intelligence/integration.py` — Add `run_backtest()` function

### Modified Files (exchange layer, if needed):
11. `arbitrage/core/market_data.py` — Add `fetch_recent_trades()` if not present

### Tests:
12. `tests/test_market_intelligence.py` — Add 25+ new tests covering all new modules

### Files to READ but NOT modify:
- `market_intelligence/models.py` — reference for data structures
- `market_intelligence/config.py` — reference for config (no new required env vars)
- `market_intelligence/statistics.py` — no changes
- `market_intelligence/validation.py` — no changes
- `market_intelligence/rate_limiter.py` — no changes
- `market_intelligence/logger.py` — no changes
- `market_intelligence/ml_weights.py` — only modify `_recompute()` method

---

## WHAT THIS ACHIEVES (10/10 Checklist)

### Trading Excellence:
- ✅ Order flow analysis (delta profiles, absorption, aggressive flow, divergence)
- ✅ Funding mean-reversion model (Ornstein-Uhlenbeck inspired, not linear)
- ✅ Liquidation cascade model (nonlinear, stage classification, directional)
- ✅ Market microstructure (spread dynamics, market impact, liquidity withdrawal)
- ✅ Backtesting framework (JSONL replay, directional accuracy, Sharpe proxy)
- ✅ ML optimizer with exponential decay (adapts to regime changes)

### IT Excellence:
- ✅ Protocol/ABC for exchange adapters (pluggable architecture)
- ✅ Observability engine (latency p95, exchange health, event log)
- ✅ Persistence versioning with migrations
- ✅ Concurrency documentation/fix for GIL
- ✅ 25+ new tests: unit, boundary, monotonicity, serialization, edge cases
- ✅ Frozen dataclasses for immutable data contracts
- ✅ Type hints everywhere with `from __future__ import annotations`
