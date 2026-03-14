from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy


class IndicatorStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(StrategyId.INDICATOR)

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        rsi = snapshot.indicators.get("rsi", 50.0)
        ema_fast = snapshot.indicators.get("ema_fast", 0.0)
        ema_slow = snapshot.indicators.get("ema_slow", 0.0)
        vwap = snapshot.indicators.get("vwap", 0.0)
        bb_upper = snapshot.indicators.get("bb_upper", 0.0)
        bb_lower = snapshot.indicators.get("bb_lower", 0.0)
        macd = snapshot.indicators.get("macd", 0.0)
        macd_signal = snapshot.indicators.get("macd_signal", 0.0)
        macd_hist = snapshot.indicators.get("macd_hist", 0.0)
        mid = next(iter(snapshot.orderbooks.values())).mid if snapshot.orderbooks else 0.0
        if mid <= 0:
            return []
        if snapshot.volatility > 3.0:
            return []

        trend_up = ema_fast > ema_slow and mid >= vwap and macd >= macd_signal
        trend_down = ema_fast < ema_slow and mid <= vwap and macd <= macd_signal
        side = ""
        edge_bps = 0.0
        if trend_up and 30.0 <= rsi <= 62.0 and macd_hist >= -1e-9:
            side = "directional_long"
            edge_bps = max(8.0, abs((mid - vwap) / max(vwap, 1e-9) * 10_000))
        elif trend_down and 38.0 <= rsi <= 58.0 and macd_hist <= 1e-9:
            side = "directional_short"
            edge_bps = max(8.0, abs((mid - vwap) / max(vwap, 1e-9) * 10_000))
        if not side:
            return []
        bb_span = max(bb_upper - bb_lower, 1e-9)
        band_pos = (mid - bb_lower) / bb_span
        if side == "directional_long" and band_pos > 0.95:
            return []
        if side == "directional_short" and band_pos < 0.05:
            return []

        exchanges = list(snapshot.orderbooks.keys())
        long_ex = exchanges[0]
        short_ex = exchanges[1] if len(exchanges) > 1 else exchanges[0]
        signal_side = 1.0 if side == "directional_long" else -1.0
        return [
            TradeIntent(
                strategy_id=self.strategy_id,
                symbol=snapshot.symbol,
                long_exchange=long_ex,
                short_exchange=short_ex,
                side=side,
                confidence=min(0.9, 0.55 + abs(macd_hist) * 5.0),
                expected_edge_bps=edge_bps,
                stop_loss_bps=20.0,
                metadata={
                    "rsi": rsi,
                    "ema_fast": ema_fast,
                    "ema_slow": ema_slow,
                    "vwap": vwap,
                    "entry_mid": mid,
                    "signal_side": signal_side,
                    "take_profit_usd": 0.10,
                    "stop_loss_usd": 0.15,
                    "max_holding_seconds": 1500.0,
                    "close_edge_bps": 0.5,
                },
            )
        ]
