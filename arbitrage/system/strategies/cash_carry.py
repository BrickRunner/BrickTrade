from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy


class CashCarryStrategy(BaseStrategy):
    def __init__(self, basis_threshold_bps: float = 12.0, safety_margin_bps: float = 1.0):
        super().__init__(StrategyId.CASH_CARRY)
        self._basis_threshold_bps = basis_threshold_bps
        self._safety_margin_bps = safety_margin_bps

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        spot_price = snapshot.indicators.get("spot_price", 0.0)
        perp_price = snapshot.indicators.get("perp_price", 0.0)
        if spot_price <= 0 or perp_price <= 0:
            return []

        basis_bps = (perp_price - spot_price) / spot_price * 10_000
        # Raise threshold in high-volatility conditions to avoid chasing unstable basis.
        vol_buffer = min(8.0, snapshot.volatility * 50.0)
        threshold = self._basis_threshold_bps + self._safety_margin_bps + vol_buffer
        if abs(basis_bps) <= threshold:
            return []

        exchanges = list(snapshot.orderbooks.keys())
        if len(exchanges) < 2:
            return []
        spot_books = snapshot.spot_orderbooks or {}
        if not spot_books:
            return []
        # Spot leg: best ask among spot books. Perp leg: best bid among perp books.
        long_ex = min(spot_books.keys(), key=lambda ex: spot_books[ex].ask)
        short_ex = max(exchanges, key=lambda ex: snapshot.orderbooks[ex].bid)
        if long_ex == short_ex:
            return []

        # Funding sanity: avoid trading against strong funding pressure on chosen legs.
        funding_long = snapshot.funding_rates.get(long_ex, 0.0) * 10_000
        funding_short = snapshot.funding_rates.get(short_ex, 0.0) * 10_000

        if basis_bps <= 0:
            # Backwardation requires spot short (borrow/margin). Skip unless margin-enabled.
            return []
        # Contango: buy spot, short perp.
        side = "long_spot_short_perp"

        funding_penalty = max(0.0, funding_long) + max(0.0, -funding_short)
        effective_edge = abs(basis_bps) - funding_penalty
        if effective_edge <= threshold:
            return []
        confidence = min(1.0, effective_edge / max(threshold * 2.0, 1e-9))

        return [
            TradeIntent(
                strategy_id=self.strategy_id,
                symbol=snapshot.symbol,
                long_exchange=long_ex,
                short_exchange=short_ex,
                side=side,
                confidence=confidence,
                expected_edge_bps=effective_edge,
                stop_loss_bps=max(self._basis_threshold_bps / 2, threshold * 0.6),
                metadata={
                    "basis_bps": basis_bps,
                    "effective_basis_bps": effective_edge,
                    "funding_long_bps": funding_long,
                    "funding_short_bps": funding_short,
                    "entry_mid": (spot_price + perp_price) / 2,
                    "basis_direction": 1.0 if basis_bps > 0 else -1.0,
                    "spot_price": spot_books[long_ex].ask,
                    "perp_price": snapshot.orderbooks[short_ex].bid,
                    "leg_kinds": {long_ex: "spot", short_ex: "perp"},
                    "take_profit_usd": 0.12,
                    "stop_loss_usd": 0.16,
                    "max_holding_seconds": 1800.0,
                    "close_edge_bps": 0.6,
                },
            )
        ]
