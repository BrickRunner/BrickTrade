"""
Triangular Arbitrage Strategy.

Exploits price inefficiencies across 3 trading pairs on a SINGLE exchange.
Example: USDT -> BTC -> ETH -> USDT
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from arbitrage.system.models import (
    MarketSnapshot,
    OrderBookSnapshot,
    StrategyId,
    TradeIntent,
)
from arbitrage.system.strategies.base import BaseStrategy

logger = logging.getLogger("trading_system")

_COMMON_TRIANGLES: List[Tuple[str, str, str]] = [
    ("USDT", "BTC", "ETH"),
    ("USDT", "BTC", "SOL"),
    ("USDT", "BTC", "BNB"),
    ("USDT", "BTC", "XRP"),
    ("USDT", "BTC", "DOGE"),
    ("USDT", "BTC", "AVAX"),
    ("USDT", "BTC", "LINK"),
    ("USDT", "BTC", "DOT"),
    ("USDT", "BTC", "LTC"),
    ("USDT", "BTC", "ADA"),
    ("USDT", "ETH", "SOL"),
    ("USDT", "ETH", "BNB"),
    ("USDT", "ETH", "LINK"),
    ("USDT", "ETH", "AVAX"),
]


def _make_pair(a: str, b: str) -> str:
    return f"{a}{b}"


class TriangularArbitrageStrategy(BaseStrategy):
    """
    Triangular arbitrage on a single exchange using spot pairs.
    Scans predefined triangle paths for price inefficiencies.
    """

    def __init__(
        self,
        min_profit_bps: float = 3.0,
        max_profit_bps: float = 200.0,
        fee_per_leg_pct: float = 0.10,
        maker_fee_per_leg_pct: float = 0.02,
        use_maker_legs: int = 2,
        cooldown_sec: float = 5.0,
        preferred_exchange: str = "bybit",
        # FIX #4: Allow raising minimum profit threshold to exceed fee costs.
        # The default is kept low for backward compat, but users should set
        # min_profit_bps >= total_fee_bps (typically 15-30 bps for retail).
        enforce_min_profit_above_fees: bool = True,
    ):
        super().__init__(StrategyId.TRIANGULAR)
        self.min_profit_bps = min_profit_bps
        self.max_profit_bps = max_profit_bps
        self.fee_per_leg_pct = fee_per_leg_pct
        self.maker_fee_per_leg_pct = maker_fee_per_leg_pct
        self.use_maker_legs = use_maker_legs
        self.cooldown_sec = cooldown_sec
        self.preferred_exchange = preferred_exchange
        self._last_signal_ts: Dict[str, float] = {}

    def _total_fee_bps(self, snapshot: MarketSnapshot | None = None, exchange: str | None = None) -> float:
        """Calculate total fee in bps for 3 legs.

        If snapshot and exchange are provided, uses exchange-specific fee rates
        from snapshot.fee_bps (spot rates). Falls back to configured defaults.
        """
        # Try to use real exchange fees from snapshot
        if snapshot is not None and exchange is not None:
            fee_data = snapshot.fee_bps.get(exchange, {})
            spot_fee_bps = 0.0
            if "spot" in fee_data:
                spot_fee_bps = abs(float(fee_data["spot"]))
            elif "perp" in fee_data:
                # Fallback to perp fees if spot not available
                spot_fee_bps = abs(float(fee_data["perp"]))
            if spot_fee_bps > 0:
                # Use real fee: maker for use_maker_legs legs, taker for rest
                maker_legs = min(self.use_maker_legs, 3)
                taker_legs = 3 - maker_legs
                # Approximate maker fee as 40% of taker (typical exchange ratio)
                maker_bps = spot_fee_bps * 0.4
                return maker_legs * maker_bps + taker_legs * spot_fee_bps

        # Default: use configured per-leg fees
        maker_legs = min(self.use_maker_legs, 3)
        taker_legs = 3 - maker_legs
        total = (maker_legs * self.maker_fee_per_leg_pct + taker_legs * self.fee_per_leg_pct) * 100
        return total

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        intents: List[TradeIntent] = []
        spot_obs = snapshot.spot_orderbooks
        if not spot_obs:
            return intents

        now = time.time()
        symbol = snapshot.symbol
        base_coin, quote_coin = self._parse_pair(symbol)
        if not base_coin or not quote_coin:
            return intents

        for exchange in spot_obs:
            cooldown_key = f"tri_{exchange}_{symbol}"
            if now - self._last_signal_ts.get(cooldown_key, 0.0) < self.cooldown_sec:
                continue
            intent = self._scan_triangles(snapshot, exchange, base_coin, quote_coin, now)
            if intent:
                self._last_signal_ts[cooldown_key] = now
                intents.append(intent)

        return intents

    def _scan_triangles(
        self, snapshot: MarketSnapshot, exchange: str,
        base_coin: str, quote_coin: str, now: float,
    ) -> Optional[TradeIntent]:
        best_intent: Optional[TradeIntent] = None
        best_profit = 0.0

        for tri_base, tri_mid, tri_quote in _COMMON_TRIANGLES:
            currencies = {tri_base, tri_mid, tri_quote}
            if base_coin not in currencies and quote_coin not in currencies:
                continue

            profit_fwd = self._calc_profit(snapshot, exchange, tri_base, tri_mid, tri_quote, True, use_real_fees=True)
            profit_rev = self._calc_profit(snapshot, exchange, tri_base, tri_mid, tri_quote, False, use_real_fees=True)

            best_dir = "forward" if profit_fwd >= profit_rev else "reverse"
            profit = max(profit_fwd, profit_rev)
            profit_bps = profit * 10000

            if profit_bps < self.min_profit_bps or profit_bps > self.max_profit_bps:
                continue

            if profit_bps > best_profit:
                best_profit = profit_bps
                pair_1 = _make_pair(tri_mid, tri_base)
                pair_2 = _make_pair(tri_quote, tri_mid)
                pair_3 = _make_pair(tri_quote, tri_base)
                confidence = min(1.0, profit_bps / 30.0)

                best_intent = TradeIntent(
                    strategy_id=StrategyId.TRIANGULAR,
                    symbol=snapshot.symbol,
                    long_exchange=exchange,
                    short_exchange=exchange,
                    side=f"triangular_{best_dir}",
                    confidence=confidence,
                    expected_edge_bps=profit_bps,
                    stop_loss_bps=profit_bps * 2,
                    metadata={
                        "strategy_type": "triangular",
                        "direction": best_dir,
                        "triangle": f"{tri_base}->{tri_mid}->{tri_quote}",
                        "pair_1": pair_1, "pair_2": pair_2, "pair_3": pair_3,
                        "profit_pct": profit * 100,
                        "profit_bps": profit_bps,
                        "exchange": exchange,
                        "fee_total_bps": self._total_fee_bps(),
                    },
                )

        if best_intent:
            m = best_intent.metadata
            logger.info(
                "[TRIANGULAR] %s on %s: %s profit=%.1f bps, fees=%.1f bps",
                snapshot.symbol, exchange, m.get("triangle", ""),
                best_profit, m.get("fee_total_bps", 0),
            )
        return best_intent

    def _calc_profit(
        self, snapshot: MarketSnapshot, exchange: str,
        tri_base: str, tri_mid: str, tri_quote: str, forward: bool,
        use_real_fees: bool = False,
    ) -> float:
        """
        Calculate profit for a triangular path.

        Forward: BASE -> MID -> QUOTE -> BASE
          Leg 1: Buy MID/BASE at ask
          Leg 2: Buy QUOTE/MID at ask
          Leg 3: Sell QUOTE/BASE at bid

        Reverse: BASE -> QUOTE -> MID -> BASE
          Leg 1: Buy QUOTE/BASE at ask
          Leg 2: Sell QUOTE/MID at bid
          Leg 3: Sell MID/BASE at bid

        Returns profit as fraction (0.001 = 0.1%).
        """
        pair_1 = _make_pair(tri_mid, tri_base)   # e.g. BTCUSDT
        pair_2 = _make_pair(tri_quote, tri_mid)   # e.g. ETHBTC
        pair_3 = _make_pair(tri_quote, tri_base)  # e.g. ETHUSDT

        ob_1 = self._get_spot_ob(snapshot, exchange, pair_1)
        ob_2 = self._get_spot_ob(snapshot, exchange, pair_2)
        ob_3 = self._get_spot_ob(snapshot, exchange, pair_3)

        if not ob_1 or not ob_2 or not ob_3:
            return 0.0

        if use_real_fees:
            fee_mult = 1.0 - self._total_fee_bps(snapshot, exchange) / 10000.0
        else:
            fee_mult = 1.0 - self._total_fee_bps() / 10000.0

        if forward:
            if ob_1.ask <= 0 or ob_2.ask <= 0 or ob_3.bid <= 0:
                return 0.0
            # Start with 1 unit of BASE
            mid_amount = 1.0 / ob_1.ask        # buy MID
            quote_amount = mid_amount / ob_2.ask  # buy QUOTE with MID... wait
            # Actually for pair ETHBTC: ask = price of ETH in BTC
            # To buy ETH with BTC: spend BTC, get ETH
            # mid_amount is in MID (BTC), pair_2 is QUOTE/MID (ETHBTC)
            # ask of ETHBTC = how many BTC for 1 ETH
            # So: quote_amount = mid_amount / ob_2.ask
            quote_amount = mid_amount / ob_2.ask
            # Sell QUOTE for BASE: bid of QUOTE/BASE
            final_base = quote_amount * ob_3.bid
            profit = (final_base * fee_mult) - 1.0
        else:
            if ob_3.ask <= 0 or ob_2.bid <= 0 or ob_1.bid <= 0:
                return 0.0
            # Reverse: BASE -> QUOTE -> MID -> BASE
            quote_amount = 1.0 / ob_3.ask       # buy QUOTE with BASE
            mid_amount = quote_amount * ob_2.bid  # sell QUOTE for MID
            final_base = mid_amount * ob_1.bid    # sell MID for BASE
            profit = (final_base * fee_mult) - 1.0

        return max(profit, 0.0)

    def _get_spot_ob(
        self, snapshot: MarketSnapshot, exchange: str, pair: str,
    ) -> Optional[OrderBookSnapshot]:
        """Get spot orderbook for a pair from snapshot."""
        # The snapshot is per-symbol, but spot_orderbooks may have data
        # for the exchange. For triangular arb we need multi-symbol data.
        # In the current architecture, we match by symbol name.
        if snapshot.symbol == pair:
            ob = snapshot.spot_orderbooks.get(exchange)
            if ob and ob.bid > 0 and ob.ask > 0:
                return ob
        # Fallback: check if orderbook_depth has the pair
        # (future: engine should pass multi-symbol spot data)
        return None

    @staticmethod
    def _parse_pair(symbol: str) -> Tuple[str, str]:
        """Parse a trading pair into base and quote currencies."""
        # Common quote currencies, ordered by length (longest first)
        quotes = ["USDT", "USDC", "BUSD", "BTC", "ETH", "BNB"]
        for q in quotes:
            if symbol.endswith(q) and len(symbol) > len(q):
                base = symbol[: -len(q)]
                return base, q
        return "", ""
