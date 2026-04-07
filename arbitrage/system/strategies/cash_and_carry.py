"""
Spot-Futures Cash & Carry Arbitrage Strategy.

Mechanism:
  - BUY spot + SHORT perpetual futures on the SAME exchange
  - Collect positive funding rate payments (shorts receive funding when rate > 0)
  - Delta-neutral: spot long hedges futures short
  - Close when funding rate turns negative or target profit reached

Advantages:
  - Works on a single exchange (no cross-exchange risk)
  - No execution timing risk (both legs on same exchange)
  - Predictable income from funding payments
  - Lower fees (one exchange, can use maker orders)

Requirements:
  - Exchange must support both spot and USDT-margined perpetuals
  - Positive funding rate (shorts receive payment)
  - Sufficient spot + futures liquidity
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot, StrategyId, TradeIntent
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.strategies.base import BaseStrategy

logger = logging.getLogger("trading_system")

# Annualized funding rate threshold (in %) — minimum to justify entry
# Typical perpetual funding: 0.01% per 8h = ~0.03% daily = ~10.95% APR
_MIN_FUNDING_APR_PCT = 5.0  # 5% APR minimum

# FIX #2: Correct taker fee rates per exchange (VIP-0, as of Mar-2026):
#   Binance spot: 0.10% taker  |  Binance futures: 0.04% taker
#   Bybit spot:  0.10% taker  |  Bybit linear:    0.055% taker
#   OKX spot:    0.08% taker  |  OKX swap:        0.05% taker
#   HTX spot:    0.20% taker  |  HTX linear swap: 0.05% taker (unified margin)
_DEFAULT_SPOT_FEE_PCT: Dict[str, float] = {
    "binance": 0.10,
    "bybit": 0.10,
    "okx": 0.08,
    "htx": 0.20,
}
_DEFAULT_PERP_FEE_PCT: Dict[str, float] = {
    "binance": 0.04,
    "bybit": 0.055,
    "okx": 0.05,
    "htx": 0.05,
}


class CashAndCarryStrategy(BaseStrategy):
    """
    Single-exchange cash & carry: buy spot + short perp, collect funding.

    Scans each exchange for positive funding rates. When funding rate APR
    exceeds threshold minus round-trip costs, generates a trade intent
    with legs: spot buy + perp short.
    """

    def __init__(
        self,
        min_funding_apr_pct: float = 5.0,
        max_basis_spread_pct: float = 0.30,
        min_holding_hours: float = 8.0,
        max_holding_hours: float = 72.0,
        min_book_depth_usd: float = 5000.0,
    ):
        super().__init__(StrategyId.CASH_AND_CARRY)

        # Minimum annualized funding rate to enter (after fees)
        self.min_funding_apr_pct = min_funding_apr_pct
        # Max acceptable basis spread (spot-futures price difference)
        self.max_basis_spread_pct = max_basis_spread_pct
        # Hold for at least one funding period
        self.min_holding_hours = min_holding_hours
        # Max holding time before forced exit
        self.max_holding_hours = max_holding_hours
        # Minimum orderbook depth on both sides
        self.min_book_depth_usd = min_book_depth_usd

        # Cooldown per (exchange, symbol)
        self._last_signal_ts: Dict[str, float] = {}
        self._signal_cooldown_sec = 300.0  # 5 min cooldown (funding changes slowly)

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        """Scan each exchange for cash & carry opportunities."""
        intents: List[TradeIntent] = []

        # Need both spot and futures orderbooks on the same exchange
        for exchange in snapshot.orderbooks:
            intent = self._check_cash_and_carry(snapshot, exchange)
            if intent:
                intents.append(intent)

        return intents

    def _check_cash_and_carry(
        self,
        snapshot: MarketSnapshot,
        exchange: str,
    ) -> Optional[TradeIntent]:
        """Check if cash & carry is profitable on a single exchange."""
        # Need futures orderbook
        perp_ob = snapshot.orderbooks.get(exchange)
        if not perp_ob or perp_ob.ask <= 0 or perp_ob.bid <= 0:
            return None

        # Need spot orderbook on same exchange
        spot_ob = snapshot.spot_orderbooks.get(exchange)
        if not spot_ob or spot_ob.ask <= 0 or spot_ob.bid <= 0:
            return None

        # Need funding rate
        funding_rate = snapshot.funding_rates.get(exchange)
        if funding_rate is None:
            return None

        # Only enter when funding is positive (shorts receive payment)
        if funding_rate <= 0:
            return None

        # Convert 8h funding rate to APR
        funding_rate_pct = funding_rate * 100  # to percent
        daily_rate = funding_rate_pct * 3  # 3 periods per day
        annual_rate = daily_rate * 365

        # Calculate basis: spot price vs futures price
        # Positive basis = futures premium (normal for positive funding)
        spot_price = spot_ob.ask  # we buy spot at ask
        perp_price = perp_ob.bid  # we short perp at bid
        basis_pct = (perp_price - spot_price) / spot_price * 100

        # If futures trade at big discount to spot, no carry trade
        if basis_pct < -self.max_basis_spread_pct:
            return None

        # Round-trip fees: spot buy + spot sell + perp open + perp close
        spot_fee = self._get_spot_fee_pct(exchange, snapshot)
        perp_fee = self._get_perp_fee_pct(exchange, snapshot)
        round_trip_fees_pct = (spot_fee + perp_fee) * 2

        # FIX #3: Correct cash & carry APR math.
        # The previous formula was mathematically broken:
        #   one_period_gain = funding - (round_trip_fees / 3)
        #   net_apr = one_period_gain × 3 × 365 = funding×1095 - fees×365
        # This multiplied the fee drag by 365 instead of the correct subtraction.
        #
        # Correct approach:
        #   Funding income per 8h period = funding_rate_pct (per leg)
        #   Total funding income over holding period = funding_rate_pct × num_periods
        #   Net profit = funding_income - round_trip_fees (one-time cost)
        #   APR = (net_profit / holding_days) × 365
        #
        # With ~3 funding periods per day and minimum holding of
        # self.min_holding_hours, the minimum number of periods is:
        min_periods = max(1, int(self.min_holding_hours / 8))
        total_funding_income_pct = funding_rate_pct * min_periods * 3  # 3 periods/day
        net_profit_pct = total_funding_income_pct - round_trip_fees_pct
        # Project to APR: net profit over min_holding_hours → annualized
        holding_days = max(self.min_holding_hours / 24.0, 1.0 / 365.0)
        net_apr = (net_profit_pct / holding_days) * 365 if holding_days > 0 else 0.0

        if net_apr < self.min_funding_apr_pct:
            logger.debug(
                "[CASH_CARRY_SKIP] %s on %s: funding_apr=%.1f%% net_apr=%.1f%% "
                "fees=%.3f%% basis=%.3f%%",
                snapshot.symbol, exchange, annual_rate, net_apr,
                round_trip_fees_pct, basis_pct,
            )
            return None

        # Depth check
        if not self._check_depth(snapshot, exchange):
            return None

        # Cooldown
        pair_key = f"cash_carry_{exchange}_{snapshot.symbol}"
        now = time.time()
        if now - self._last_signal_ts.get(pair_key, 0) < self._signal_cooldown_sec:
            return None

        self._last_signal_ts[pair_key] = now

        # Confidence based on how much APR exceeds threshold
        confidence = min(1.0, net_apr / max(self.min_funding_apr_pct * 3, 1.0))

        # FIX #3 (continued): edge_bps = net profit in percent × 100 = bps.
        # The old code used `one_period_gain_pct` which no longer exists
        # after the APR math rewrite.  Use net_profit_pct directly.
        edge_bps = net_profit_pct * 100  # pct → bps

        logger.info(
            "[CASH_CARRY_SIGNAL] %s on %s: funding=%.4f%% apr=%.1f%% net_apr=%.1f%% "
            "basis=%.3f%% spot=%.2f perp=%.2f fees=%.3f%%",
            snapshot.symbol, exchange, funding_rate_pct, annual_rate, net_apr,
            basis_pct, spot_price, perp_price, round_trip_fees_pct,
        )

        return TradeIntent(
            strategy_id=self.strategy_id,
            symbol=snapshot.symbol,
            long_exchange=exchange,   # spot buy
            short_exchange=exchange,  # perp short (same exchange)
            side="cash_and_carry",
            confidence=confidence,
            expected_edge_bps=edge_bps,
            stop_loss_bps=50.0,  # wide SL — this is a carry trade
            metadata={
                "arb_type": "cash_and_carry",
                "exchange": exchange,
                "spot_price": spot_price,
                "perp_price": perp_price,
                "basis_pct": round(basis_pct, 4),
                "funding_rate_pct": round(funding_rate_pct, 4),
                "funding_apr_pct": round(annual_rate, 2),
                "net_apr_pct": round(net_apr, 2),
                "round_trip_fees_pct": round(round_trip_fees_pct, 4),
                "entry_mid": (spot_price + perp_price) / 2,
                "entry_long_price": spot_price,
                "entry_short_price": perp_price,
                "long_price": spot_price,
                "short_price": perp_price,
                # Execution config
                "leg_kinds": {exchange: "spot"},  # long leg = spot
                "max_holding_seconds": int(self.max_holding_hours * 3600),
                "take_profit_pct": round(net_apr / 100.0 / 365 * self.min_holding_hours / 24.0, 6),
                "close_edge_bps": 0.0,  # don't exit on edge — exit on funding/time
                # Limit prices with buffer
                "limit_prices": {
                    "buy": spot_price,   # spot buy
                    "sell": perp_price,  # perp short
                },
                # For spot leg sizing
                "spot_price": spot_price,
            },
        )

    def _check_depth(self, snapshot: MarketSnapshot, exchange: str) -> bool:
        """Check both spot and perp depth are sufficient."""
        # Check perp depth
        perp_depth = snapshot.orderbook_depth.get(exchange)
        if perp_depth:
            bids = perp_depth.get("bids", [])
            asks = perp_depth.get("asks", [])
            if bids and asks:
                bid_usd = sum(float(b[0]) * float(b[1]) for b in bids[:5] if len(b) >= 2)
                ask_usd = sum(float(a[0]) * float(a[1]) for a in asks[:5] if len(a) >= 2)
                if min(bid_usd, ask_usd) < self.min_book_depth_usd:
                    return False

        # Check spot depth
        spot_depth = snapshot.spot_orderbook_depth.get(exchange)
        if spot_depth:
            bids = spot_depth.get("bids", [])
            asks = spot_depth.get("asks", [])
            if bids and asks:
                bid_usd = sum(float(b[0]) * float(b[1]) for b in bids[:5] if len(b) >= 2)
                ask_usd = sum(float(a[0]) * float(a[1]) for a in asks[:5] if len(a) >= 2)
                if min(bid_usd, ask_usd) < self.min_book_depth_usd:
                    return False

        return True

    def _get_spot_fee_pct(self, exchange: str, snapshot: MarketSnapshot) -> float:
        """Get spot taker fee percentage."""
        fee_data = snapshot.fee_bps.get(exchange, {})
        if "spot" in fee_data:
            val = abs(float(fee_data["spot"]))
            if val > 0:
                return val / 100  # bps → percent
        return _DEFAULT_SPOT_FEE_PCT.get(exchange, 0.10)

    def _get_perp_fee_pct(self, exchange: str, snapshot: MarketSnapshot) -> float:
        """Get perpetual taker fee percentage."""
        fee_data = snapshot.fee_bps.get(exchange, {})
        if "perp" in fee_data:
            val = abs(float(fee_data["perp"]))
            if val > 0:
                return val / 100
        return _DEFAULT_PERP_FEE_PCT.get(exchange, 0.05)
