from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.fees import fee_bps_from_snapshot
from arbitrage.system.strategies.base import BaseStrategy


class SpreadCaptureStrategy(BaseStrategy):
    def __init__(self, min_spread_bps: float = 6.0, price_improve_bps: float = 0.5):
        super().__init__(StrategyId.SPREAD_CAPTURE)
        self._min_spread_bps = min_spread_bps
        self._price_improve_bps = price_improve_bps

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        orderbooks = snapshot.spot_orderbooks or {}
        if not orderbooks:
            return []
        intents: List[TradeIntent] = []
        for exchange, ob in orderbooks.items():
            if ob.bid <= 0 or ob.ask <= 0:
                continue
            mid = ob.mid
            spread_bps = (ob.ask - ob.bid) / max(mid, 1e-9) * 10_000
            fees = fee_bps_from_snapshot(snapshot, exchange, "spot", snapshot.symbol) * 2
            if spread_bps - fees < self._min_spread_bps:
                continue
            # Place IOC limit orders slightly inside spread.
            improve = (self._price_improve_bps / 10_000) * mid
            buy_px = min(ob.ask, ob.bid + improve)
            sell_px = max(ob.bid, ob.ask - improve)
            if buy_px >= sell_px:
                continue
            intents.append(
                TradeIntent(
                    strategy_id=self.strategy_id,
                    symbol=snapshot.symbol,
                    long_exchange=exchange,
                    short_exchange=exchange,
                    side="spread_capture",
                    confidence=min(1.0, spread_bps / max(self._min_spread_bps * 2, 1e-9)),
                    expected_edge_bps=spread_bps - fees,
                    stop_loss_bps=max(self._min_spread_bps, spread_bps * 0.6),
                    metadata={
                        "leg_kinds": {exchange: "spot"},
                        "limit_prices": {
                            "buy": buy_px,
                            "sell": sell_px,
                        },
                        "long_price": buy_px,
                        "short_price": sell_px,
                        "entry_mid": mid,
                        "take_profit_usd": 0.04,
                        "stop_loss_usd": 0.08,
                        "max_holding_seconds": 120.0,
                        "close_edge_bps": 0.2,
                    },
                )
            )
        return intents
