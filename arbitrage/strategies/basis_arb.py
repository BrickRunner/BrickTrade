"""
Strategy B: Basis Arbitrage (Cash & Carry)

Captures futures premium relative to spot price.

Detection:
    basis = (futures_price - spot_price) / spot_price * 100

Entry condition:
    basis > fees + slippage + safety_buffer

Execution:
    Cash & Carry (basis > 0):
        - Buy equivalent on low-premium exchange (long leg)
        - Sell on high-premium exchange (short leg)
    Reverse (basis < 0):
        - Opposite direction

Since we use perpetual futures (no expiry), basis profit comes from
the premium converging, which typically happens within hours/days.

Exit:
    - basis < close_threshold
    - timeout
    - risk trigger

Supported combinations (per symbol):
    - OKX spot vs OKX futures
    - HTX spot vs HTX futures
    - Bybit spot vs Bybit futures
    - Cross-exchange: OKX spot vs HTX futures, etc.
"""
from typing import List, Optional, Tuple, Dict

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.market_data import MarketDataEngine
from arbitrage.core.state import ActivePosition
from arbitrage.strategies.base import BaseStrategy, Opportunity, StrategyType

logger = get_arbitrage_logger("basis_arb")

# Round-trip fees: spot taker 0.1% + perp taker 0.06% ≈ 0.16% per side
ROUND_TRIP_FEE_PCT = 0.32
SAFETY_BUFFER_PCT = 0.05


class BasisArbStrategy(BaseStrategy):

    def __init__(self, config: ArbitrageConfig, market_data: MarketDataEngine):
        self.config = config
        self.market_data = market_data
        self._min_basis = config.min_basis
        self._close_threshold = config.basis_close_threshold

    @property
    def name(self) -> str:
        return "basis_arb"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.BASIS_ARB

    def get_threshold(self, _symbol: str) -> float:
        return self._min_basis

    async def detect_opportunities(self, market_data: MarketDataEngine) -> List[Opportunity]:
        """
        For each symbol, compare spot vs futures across all exchange combinations.
        Return opportunities where basis > min_threshold.
        """
        opportunities: List[Opportunity] = []
        exchanges = market_data.get_exchange_names()

        for ex in exchanges:
            for sym in market_data.common_pairs:
                spot = market_data.get_spot_price(ex, sym)
                futures = market_data.get_futures_price(ex, sym)
                if not spot or not futures:
                    continue

                opp = self._check_basis(sym, spot, futures.ask, futures.bid, ex, ex)
                if opp:
                    opportunities.append(opp)

        # Cross-exchange basis: spot on ex1, futures on ex2
        for i, ex1 in enumerate(exchanges):
            for ex2 in exchanges[i + 1:]:
                for sym in market_data.common_pairs:
                    # ex1 spot vs ex2 futures
                    spot1 = market_data.get_spot_price(ex1, sym)
                    fut2 = market_data.get_futures_price(ex2, sym)
                    if spot1 and fut2:
                        opp = self._check_basis(sym, spot1, fut2.ask, fut2.bid, ex1, ex2)
                        if opp:
                            opportunities.append(opp)

                    # ex2 spot vs ex1 futures
                    spot2 = market_data.get_spot_price(ex2, sym)
                    fut1 = market_data.get_futures_price(ex1, sym)
                    if spot2 and fut1:
                        opp = self._check_basis(sym, spot2, fut1.ask, fut1.bid, ex2, ex1)
                        if opp:
                            opportunities.append(opp)

        # Deduplicate: keep best per symbol
        best: Dict[str, Opportunity] = {}
        for opp in opportunities:
            key = f"{opp.symbol}_{opp.long_exchange}_{opp.short_exchange}"
            if key not in best or opp.expected_profit_pct > best[key].expected_profit_pct:
                best[key] = opp

        result = sorted(best.values(), key=lambda o: o.expected_profit_pct, reverse=True)
        return result

    def _check_basis(
        self,
        symbol: str,
        spot_price: float,
        futures_ask: float,
        futures_bid: float,
        spot_exchange: str,
        futures_exchange: str,
    ) -> Optional[Opportunity]:
        """Check if basis between spot and futures is profitable."""
        if spot_price <= 0 or futures_ask <= 0:
            return None

        # Cash & carry: futures premium
        basis = (futures_bid - spot_price) / spot_price * 100
        abs_basis = abs(basis)

        min_required = ROUND_TRIP_FEE_PCT + SAFETY_BUFFER_PCT
        if abs_basis < max(self._min_basis, min_required):
            return None

        net_profit = abs_basis - ROUND_TRIP_FEE_PCT

        if basis > 0:
            # Futures premium: buy spot (long), sell futures (short)
            return Opportunity(
                strategy=StrategyType.BASIS_ARB,
                symbol=symbol,
                long_exchange=spot_exchange,
                short_exchange=futures_exchange,
                expected_profit_pct=net_profit,
                long_price=spot_price,
                short_price=futures_bid,
                metadata={
                    "basis_pct": basis,
                    "direction": "cash_and_carry",
                    "spot_exchange": spot_exchange,
                    "futures_exchange": futures_exchange,
                },
            )
        else:
            # Futures discount: sell spot (short), buy futures (long)
            return Opportunity(
                strategy=StrategyType.BASIS_ARB,
                symbol=symbol,
                long_exchange=futures_exchange,
                short_exchange=spot_exchange,
                expected_profit_pct=net_profit,
                long_price=futures_ask,
                short_price=spot_price,
                metadata={
                    "basis_pct": basis,
                    "direction": "reverse_cash_carry",
                    "spot_exchange": spot_exchange,
                    "futures_exchange": futures_exchange,
                },
            )

    async def should_exit(
        self, position: ActivePosition, market_data: MarketDataEngine
    ) -> Tuple[bool, str]:
        """
        Exit when basis converged or timeout.
        """
        sym = position.symbol
        # Try to reconstruct basis from position metadata
        spot_ex = position.long_exchange  # In cash_and_carry, long = spot
        fut_ex = position.short_exchange

        spot = market_data.get_spot_price(spot_ex, sym)
        fut = market_data.get_futures_price(fut_ex, sym)

        if spot and fut:
            current_basis = abs((fut.bid - spot) / spot * 100)
            if current_basis <= self._close_threshold:
                return True, "basis_converged"

        # Timeout: 24h
        if position.duration() > 24 * 3600:
            return True, "timeout_24h"

        return False, ""

    def get_all_spreads(self, market_data: MarketDataEngine) -> list:
        """All basis values for display."""
        items = []
        for ex in market_data.get_exchange_names():
            for sym in market_data.common_pairs:
                spot = market_data.get_spot_price(ex, sym)
                fut = market_data.get_futures_price(ex, sym)
                if spot and fut and spot > 0:
                    basis = (fut.last - spot) / spot * 100
                    items.append({
                        "symbol": sym,
                        "basis_pct": basis,
                        "exchange": ex,
                        "spot_price": spot,
                        "futures_price": fut.last,
                    })
        items.sort(key=lambda x: abs(x["basis_pct"]), reverse=True)
        return items
