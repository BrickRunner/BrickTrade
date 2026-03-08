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
from dataclasses import dataclass
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
