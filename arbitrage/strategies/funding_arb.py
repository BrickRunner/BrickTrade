"""
Strategy A: Funding Rate Arbitrage

Market-neutral profit from funding rate differences across exchanges.

Logic:
- For each symbol, find the exchange pair with the largest funding rate spread
- LONG on exchange with lowest funding (we receive or pay less)
- SHORT on exchange with highest funding (we receive more)
- Hold until accumulated funding profit >= target OR spread collapses

Detection:
    funding_spread = max_funding - min_funding (across 3 exchanges)
    if funding_spread > dynamic_threshold → opportunity

Dynamic thresholds:
    BTC: 0.02%
    ETH: 0.03%
    ALT: 0.05%

Exit conditions:
    1. accumulated_funding >= target_profit
    2. funding_spread < exit_threshold (spread collapsed)
    3. Risk engine triggers exit
"""
from typing import List, Tuple

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.market_data import MarketDataEngine
from arbitrage.core.state import ActivePosition
from arbitrage.strategies.base import BaseStrategy, Opportunity, StrategyType

logger = get_arbitrage_logger("funding_arb")

# Round-trip fees (open + close, both legs)
ROUND_TRIP_FEE_PCT = 0.08  # 4 legs × 0.02% maker ≈ 0.08%


class FundingArbStrategy(BaseStrategy):

    def __init__(self, config: ArbitrageConfig, market_data: MarketDataEngine):
        self.config = config
        self.market_data = market_data
        self._btc_thr = config.funding_btc_threshold
        self._eth_thr = config.funding_eth_threshold
        self._alt_thr = config.funding_alt_threshold
        self._target_profit = config.funding_target_profit
        self._exit_factor = 0.5  # Exit when spread < entry_threshold * factor

    @property
    def name(self) -> str:
        return "funding_arb"

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.FUNDING_ARB

    def get_threshold(self, symbol: str) -> float:
        if symbol.startswith("BTC"):
            return self._btc_thr
        elif symbol.startswith("ETH"):
            return self._eth_thr
        return self._alt_thr

    async def detect_opportunities(self, market_data: MarketDataEngine) -> List[Opportunity]:
        """
        For each symbol present on >= 2 exchanges, find the max funding spread.
        """
        opportunities: List[Opportunity] = []
        exchanges = market_data.get_exchange_names()

        # Collect all symbols with funding data on >= 2 exchanges
        symbol_rates = {}  # {symbol: [(exchange, rate_pct), ...]}
        for ex in exchanges:
            rates = market_data.funding_rates.get(ex, {})
            for sym, fd in rates.items():
                symbol_rates.setdefault(sym, []).append((ex, fd.rate_pct))

        for sym, rate_list in symbol_rates.items():
            if len(rate_list) < 2:
                continue

            # Find max and min funding rate
            rate_list.sort(key=lambda x: x[1])
            min_ex, min_rate = rate_list[0]
            max_ex, max_rate = rate_list[-1]

            funding_spread = max_rate - min_rate
            threshold = self.get_threshold(sym)

            if funding_spread < threshold:
                continue

            # Net funding per 8h interval = what we earn
            # SHORT on max_rate exchange (positive funding → shorts receive)
            # LONG on min_rate exchange (low/negative funding → longs pay less)
            net_8h = funding_spread  # % per 8h

            # Must cover fees within reasonable time
            intervals_to_cover_fees = ROUND_TRIP_FEE_PCT / net_8h if net_8h > 0 else 999
            if intervals_to_cover_fees > 6:  # > 2 days → skip
                continue

            # Check that both exchanges have futures prices (can actually trade)
            p_long = market_data.get_futures_price(min_ex, sym)
            p_short = market_data.get_futures_price(max_ex, sym)
            if not p_long or not p_short:
                continue

            annualized = net_8h * 3 * 365  # 3 intervals per day

            opportunities.append(Opportunity(
                strategy=StrategyType.FUNDING_ARB,
                symbol=sym,
                long_exchange=min_ex,
                short_exchange=max_ex,
                expected_profit_pct=net_8h,
                long_price=p_long.ask,
                short_price=p_short.bid,
                confidence=min(1.0, funding_spread / threshold),
                metadata={
                    "long_funding": min_rate,
                    "short_funding": max_rate,
                    "funding_spread": funding_spread,
                    "annualized": annualized,
                    "intervals_to_profit": intervals_to_cover_fees,
                },
            ))

        opportunities.sort(key=lambda o: o.expected_profit_pct, reverse=True)
        return opportunities

    async def should_exit(
        self, position: ActivePosition, market_data: MarketDataEngine
    ) -> Tuple[bool, str]:
        """
        Exit when:
        1. Accumulated funding profit >= target
        2. Funding spread collapsed below exit threshold
        3. Position held > 48h with no profit
        """
        sym = position.symbol
        long_ex = position.long_exchange
        short_ex = position.short_exchange

        # Check accumulated funding
        if position.accumulated_funding >= self._target_profit * position.size_usd / 100:
            return True, "funding_target_reached"

        # Check current funding spread
        long_fd = market_data.get_funding(long_ex, sym)
        short_fd = market_data.get_funding(short_ex, sym)

        if long_fd and short_fd:
            current_spread = short_fd.rate_pct - long_fd.rate_pct
            threshold = self.get_threshold(sym)
            exit_thr = threshold * self._exit_factor

            if current_spread < exit_thr:
                return True, "funding_spread_collapsed"

            # Funding reversed — now we're paying instead of earning
            if current_spread < 0:
                return True, "funding_reversed"

        # Timeout: 48h without meeting target
        if position.duration() > 48 * 3600:
            return True, "timeout_48h"

        return False, ""

    def estimate_funding_profit(
        self, position: ActivePosition, market_data: MarketDataEngine
    ) -> float:
        """Estimate funding income per 8h for a position (in USDT)."""
        fd_long = market_data.get_funding(position.long_exchange, position.symbol)
        fd_short = market_data.get_funding(position.short_exchange, position.symbol)
        if not fd_long or not fd_short:
            return 0.0
        net_rate = fd_short.rate_pct - fd_long.rate_pct
        return position.size_usd * net_rate / 100

    def get_all_spreads(self, market_data: MarketDataEngine) -> list:
        """Return all funding spreads for display."""
        exchanges = market_data.get_exchange_names()
        items = []
        symbol_rates = {}
        for ex in exchanges:
            for sym, fd in market_data.funding_rates.get(ex, {}).items():
                symbol_rates.setdefault(sym, {})[ex] = fd.rate_pct

        for sym, rates in symbol_rates.items():
            if len(rates) < 2:
                continue
            sorted_rates = sorted(rates.items(), key=lambda x: x[1])
            min_ex, min_rate = sorted_rates[0]
            max_ex, max_rate = sorted_rates[-1]
            spread = max_rate - min_rate
            items.append({
                "symbol": sym,
                "funding_spread": spread,
                "long_exchange": min_ex,
                "short_exchange": max_ex,
                "rates": rates,
                "annualized": spread * 3 * 365,
            })
        items.sort(key=lambda x: x["funding_spread"], reverse=True)
        return items
