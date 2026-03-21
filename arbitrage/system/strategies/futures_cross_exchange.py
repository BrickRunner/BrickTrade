"""
Perpetual Futures Cross-Exchange Arbitrage Strategy.

Goal: Capture risk-free or low-risk profit from:
  1. Price spread between futures on different exchanges
  2. Funding rate differences
  3. Liquidity imbalances

Mechanism:
  - LONG on exchange with lower price, SHORT on exchange with higher price
  - Close when spread collapses, target profit reached, or risk limit hit

Supported exchanges: Bybit, Binance, OKX (all pairwise combinations)
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import Dict, List, Optional

from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy

logger = logging.getLogger("trading_system")


# Default fee rates per exchange (taker, in percent)
_DEFAULT_FEE_PCT: Dict[str, float] = {
    "binance": 0.04,
    "bybit": 0.055,
    "okx": 0.05,
    "htx": 0.05,
}


class FuturesCrossExchangeStrategy(BaseStrategy):
    """
    Cross-exchange perpetual futures arbitrage.

    Scans ALL exchange pairs for spread opportunities and funding rate arbitrage.
    Opens LONG on cheap exchange + SHORT on expensive exchange.
    """

    def __init__(
        self,
        min_spread_pct: float = 0.08,
        target_profit_pct: float = 0.12,
        max_spread_risk_pct: float = 0.15,
        exit_spread_pct: float = 0.02,
        funding_threshold_pct: float = 0.01,
        max_latency_ms: float = 400.0,
        min_book_depth_multiplier: float = 3.0,
    ):
        super().__init__(StrategyId.FUTURES_CROSS_EXCHANGE)

        # Entry: spread must exceed this + total fees on both sides
        self.min_spread_pct = min_spread_pct
        # Target profit for take-profit
        self.target_profit_pct = target_profit_pct
        # Max adverse spread movement before stop-loss
        self.max_spread_risk_pct = max_spread_risk_pct
        # Exit when spread collapses to this level
        self.exit_spread_pct = exit_spread_pct
        # Funding rate threshold for funding-rate-only arbitrage
        self.funding_threshold_pct = funding_threshold_pct
        # Max API latency allowed for entry
        self.max_latency_ms = max_latency_ms
        # Orderbook depth must be >= position_size * this multiplier
        self.min_book_depth_multiplier = min_book_depth_multiplier

        # Cooldown per exchange pair to avoid rapid re-entries
        self._last_signal_ts: Dict[str, float] = {}
        self._signal_cooldown_sec = 5.0

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        """Analyze all exchange pairs for arbitrage opportunities."""
        if len(snapshot.orderbooks) < 2:
            return []

        intents: List[TradeIntent] = []

        # Check all pairwise combinations of exchanges
        exchanges = list(snapshot.orderbooks.keys())
        for ex_a, ex_b in itertools.combinations(exchanges, 2):
            ob_a = snapshot.orderbooks[ex_a]
            ob_b = snapshot.orderbooks[ex_b]

            # --- Price Spread Arbitrage ---
            # Check both directions, keep only the better one to avoid
            # contradictory intents in the same cycle.
            intent_ab = self._check_price_spread(
                snapshot, ex_a, ob_a, ex_b, ob_b
            )
            intent_ba = self._check_price_spread(
                snapshot, ex_b, ob_b, ex_a, ob_a
            )
            if intent_ab and intent_ba:
                # Keep the direction with higher expected edge
                best = intent_ab if intent_ab.expected_edge_bps >= intent_ba.expected_edge_bps else intent_ba
                intents.append(best)
            elif intent_ab:
                intents.append(intent_ab)
            elif intent_ba:
                intents.append(intent_ba)

            # --- Funding Rate Arbitrage ---
            funding_intent = self._check_funding_rate(
                snapshot, ex_a, ob_a, ex_b, ob_b
            )
            if funding_intent:
                intents.append(funding_intent)

        return intents

    def _check_price_spread(
        self,
        snapshot: MarketSnapshot,
        long_ex: str,
        long_ob: OrderBookSnapshot,
        short_ex: str,
        short_ob: OrderBookSnapshot,
    ) -> Optional[TradeIntent]:
        """
        Check if buying on long_ex and selling on short_ex is profitable.

        spread = (short_bid - long_ask) / long_ask * 100

        Entry condition: spread >= min_spread_pct + total_fees
        """
        if long_ob.ask <= 0 or short_ob.bid <= 0:
            return None

        # Calculate spread: we buy at long_ask, sell at short_bid
        spread_pct = (short_ob.bid - long_ob.ask) / long_ob.ask * 100

        # Taker fees on both legs
        fee_long = self._get_fee_pct(long_ex, snapshot)
        fee_short = self._get_fee_pct(short_ex, snapshot)
        entry_fees_pct = fee_long + fee_short  # one-way entry cost
        # Full round-trip fees (entry + exit, taker both legs both ways)
        total_fees_pct = entry_fees_pct * 2

        # Net spread after FULL round-trip fees (entry + exit)
        net_spread_pct = spread_pct - total_fees_pct

        # Entry condition: net spread must exceed minimum
        if net_spread_pct < self.min_spread_pct:
            # Log near-misses for diagnostics (net spread > 0 but below threshold)
            if net_spread_pct > 0:
                logger.debug(
                    f"[SPREAD_NEAR_MISS] {snapshot.symbol} "
                    f"LONG@{long_ex}={long_ob.ask:.4f} SHORT@{short_ex}={short_ob.bid:.4f} "
                    f"raw={spread_pct:.4f}% fees={entry_fees_pct:.4f}% "
                    f"net={net_spread_pct:.4f}% need={self.min_spread_pct:.4f}%"
                )
            return None

        # Cooldown check
        pair_key = f"{long_ex}_{short_ex}_{snapshot.symbol}"
        now = time.time()
        if now - self._last_signal_ts.get(pair_key, 0) < self._signal_cooldown_sec:
            return None

        # Liquidity check via orderbook depth
        depth_ok = self._check_depth(snapshot, long_ex, short_ex)
        if not depth_ok:
            return None

        self._last_signal_ts[pair_key] = now

        # Confidence scales with how much spread exceeds threshold
        confidence = min(1.0, net_spread_pct / max(1e-9, self.min_spread_pct * 3.0))

        # Convert to bps for the intent
        edge_bps = net_spread_pct * 100
        stop_loss_bps = self.max_spread_risk_pct * 100

        return TradeIntent(
            strategy_id=self.strategy_id,
            symbol=snapshot.symbol,
            long_exchange=long_ex,
            short_exchange=short_ex,
            side="cross_exchange_arb",
            confidence=confidence,
            expected_edge_bps=edge_bps,
            stop_loss_bps=stop_loss_bps,
            metadata={
                "spread_pct": round(spread_pct, 4),
                "net_spread_pct": round(net_spread_pct, 4),
                "total_fees_pct": round(total_fees_pct, 4),
                "long_price": long_ob.ask,
                "short_price": short_ob.bid,
                "entry_long_price": long_ob.ask,
                "entry_short_price": short_ob.bid,
                "entry_mid": (long_ob.ask + short_ob.bid) / 2,
                "target_profit_pct": self.target_profit_pct,
                "exit_spread_pct": self.exit_spread_pct,
                "max_spread_risk_pct": self.max_spread_risk_pct,
                "entry_spread_pct": spread_pct,
                "arb_type": "price_spread",
                # TP/SL as percentage of notional — engine computes USD
                "take_profit_pct": round(net_spread_pct * 0.7 / 100, 6),
                "stop_loss_pct": round(self.max_spread_risk_pct / 100, 6),
                "max_holding_seconds": 3600,
                "close_edge_bps": self.exit_spread_pct * 100,
                # Limit prices for IOC slippage buffer
                "limit_prices": {"buy": long_ob.ask, "sell": short_ob.bid},
            },
        )

    def _check_funding_rate(
        self,
        snapshot: MarketSnapshot,
        ex_a: str,
        ob_a: OrderBookSnapshot,
        ex_b: str,
        ob_b: OrderBookSnapshot,
    ) -> Optional[TradeIntent]:
        """
        Funding rate arbitrage.

        If funding_rate on one exchange > threshold:
          SHORT on exchange with positive/higher funding (receive payment)
          LONG on exchange with lower funding (pay less)
        """
        fr_a = snapshot.funding_rates.get(ex_a)
        fr_b = snapshot.funding_rates.get(ex_b)

        if fr_a is None or fr_b is None:
            return None

        # Convert to percentage
        fr_a_pct = fr_a * 100
        fr_b_pct = fr_b * 100
        fr_diff_pct = abs(fr_a_pct - fr_b_pct)

        if fr_diff_pct < self.funding_threshold_pct:
            return None

        # SHORT on exchange with higher funding rate (receive funding)
        # LONG on exchange with lower funding rate
        if fr_a_pct > fr_b_pct:
            short_ex, long_ex = ex_a, ex_b
            short_ob, long_ob = ob_a, ob_b
        else:
            short_ex, long_ex = ex_b, ex_a
            short_ob, long_ob = ob_b, ob_a

        # Check that we're not paying more in spread + fees than we earn in funding
        # Spread cost is positive when we lose money crossing (buy@ask, sell@bid)
        spread_cost_pct = (long_ob.ask - short_ob.bid) / long_ob.ask * 100
        fee_long = self._get_fee_pct(long_ex, snapshot)
        fee_short = self._get_fee_pct(short_ex, snapshot)
        # Full round-trip: entry fees + exit fees + spread crossing cost
        total_round_trip_cost = max(0.0, spread_cost_pct) + (fee_long + fee_short) * 2

        # At minimum, one funding period's income should cover round-trip costs
        if fr_diff_pct < total_round_trip_cost:
            return None

        # Cooldown
        pair_key = f"funding_{long_ex}_{short_ex}_{snapshot.symbol}"
        now = time.time()
        if now - self._last_signal_ts.get(pair_key, 0) < 60.0:  # longer cooldown for funding
            return None

        self._last_signal_ts[pair_key] = now

        confidence = min(1.0, fr_diff_pct / max(1e-9, self.funding_threshold_pct * 3.0))
        edge_bps = fr_diff_pct * 100

        return TradeIntent(
            strategy_id=self.strategy_id,
            symbol=snapshot.symbol,
            long_exchange=long_ex,
            short_exchange=short_ex,
            side="funding_arb",
            confidence=confidence * 0.8,  # slightly lower confidence for funding
            expected_edge_bps=edge_bps,
            stop_loss_bps=self.max_spread_risk_pct * 100,
            metadata={
                "funding_rate_diff_pct": round(fr_diff_pct, 4),
                "funding_long": round(snapshot.funding_rates[long_ex] * 100, 4),
                "funding_short": round(snapshot.funding_rates[short_ex] * 100, 4),
                "long_price": long_ob.ask,
                "short_price": short_ob.bid,
                "entry_long_price": long_ob.ask,
                "entry_short_price": short_ob.bid,
                "entry_mid": (long_ob.ask + short_ob.bid) / 2,
                "arb_type": "funding_rate",
                # Funding arb holds longer — TP/SL as pct of notional
                "take_profit_pct": round(fr_diff_pct * 0.6 / 100, 6),
                "stop_loss_pct": round(self.max_spread_risk_pct / 100, 6),
                "max_holding_seconds": 28800,  # 8 hours (one funding period)
                "close_edge_bps": 0.5,
                "limit_prices": {"buy": long_ob.ask, "sell": short_ob.bid},
            },
        )

    def _check_depth(
        self,
        snapshot: MarketSnapshot,
        long_ex: str,
        short_ex: str,
    ) -> bool:
        """Check orderbook depth is sufficient for the trade."""
        depth_data = snapshot.orderbook_depth
        if not depth_data:
            # If no depth data available, allow the trade (depth check is optional)
            return True

        for ex in [long_ex, short_ex]:
            ex_depth = depth_data.get(ex)
            if not ex_depth:
                continue
            bids = ex_depth.get("bids", [])
            asks = ex_depth.get("asks", [])
            if not bids or not asks:
                continue
            # Sum top levels of depth in USD
            bid_depth_usd = sum(float(b[0]) * float(b[1]) for b in bids[:5] if len(b) >= 2)
            ask_depth_usd = sum(float(a[0]) * float(a[1]) for a in asks[:5] if len(a) >= 2)
            min_depth = min(bid_depth_usd, ask_depth_usd)
            # Require minimum $1000 depth in top 5 levels
            if min_depth < 1000:
                return False

        return True

    def _get_fee_pct(self, exchange: str, snapshot: MarketSnapshot) -> float:
        """Get taker fee percentage for an exchange."""
        fee_data = snapshot.fee_bps.get(exchange, {})
        if "perp" in fee_data:
            # Use abs() because some exchanges report taker fees as negative.
            # A fee of 0 bps is suspicious for taker — fall back to defaults.
            fee_val = abs(fee_data["perp"])
            if fee_val > 0:
                return fee_val / 100  # bps to percent
        return _DEFAULT_FEE_PCT.get(exchange, 0.05)
