from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.fees import fee_bps_from_snapshot
from arbitrage.system.strategies.base import BaseStrategy


class PreFundedArbitrageStrategy(BaseStrategy):
    def __init__(self, min_edge_bps: float = 3.0, min_balance_usd: float = 5.0):
        super().__init__(StrategyId.PREFUNDED_ARBITRAGE)
        self._min_edge_bps = min_edge_bps
        self._min_balance_usd = min_balance_usd

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        exchanges = list(snapshot.orderbooks.keys())
        if len(exchanges) < 2:
            return []
        balances = snapshot.balances or {}
        best_intent: TradeIntent | None = None

        for long_ex in exchanges:
            if balances.get(long_ex, 0.0) < self._min_balance_usd:
                continue
            long_ask = snapshot.orderbooks[long_ex].ask
            for short_ex in exchanges:
                if short_ex == long_ex:
                    continue
                if balances.get(short_ex, 0.0) < self._min_balance_usd:
                    continue
                short_bid = snapshot.orderbooks[short_ex].bid
                edge_bps = (short_bid - long_ask) / max(long_ask, 1e-9) * 10_000
                fees = (
                    fee_bps_from_snapshot(snapshot, long_ex, "perp", snapshot.symbol)
                    + fee_bps_from_snapshot(snapshot, short_ex, "perp", snapshot.symbol)
                )
                net_edge = edge_bps - fees
                if net_edge < self._min_edge_bps:
                    continue
                intent = TradeIntent(
                    strategy_id=self.strategy_id,
                    symbol=snapshot.symbol,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    side="prefunded",
                    confidence=min(1.0, edge_bps / max(self._min_edge_bps * 2, 1e-9)),
                    expected_edge_bps=net_edge,
                    stop_loss_bps=max(self._min_edge_bps, edge_bps * 0.6),
                    metadata={
                        "long_price": long_ask,
                        "short_price": short_bid,
                        "entry_mid": (long_ask + short_bid) / 2,
                        "leg_kinds": {long_ex: "perp", short_ex: "perp"},
                        "limit_prices": {"buy": long_ask, "sell": short_bid},
                        "take_profit_usd": 0.10,
                        "stop_loss_usd": 0.15,
                        "max_holding_seconds": 900.0,
                        "close_edge_bps": 0.5,
                    },
                )
                if best_intent is None or intent.expected_edge_bps > best_intent.expected_edge_bps:
                    best_intent = intent

        return [best_intent] if best_intent else []
