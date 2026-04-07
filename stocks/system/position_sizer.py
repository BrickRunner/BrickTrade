"""ATR-based position sizing for stock trades.

Uses a fixed-fraction risk model: risk a configurable percentage of equity
per trade, with the stop-loss distance derived from ATR.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class StockPositionSizer:
    """Calculate position size (in lots) based on ATR risk model.

    Parameters
    ----------
    risk_per_trade_pct : float
        Fraction of equity risked per trade (e.g. 0.01 = 1%).
    sl_atr_mult : float
        Stop-loss distance expressed as a multiple of ATR.
    """

    def __init__(
        self,
        risk_per_trade_pct: float = 0.01,
        sl_atr_mult: float = 2.0,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.sl_atr_mult = sl_atr_mult

    def calculate_lots(
        self,
        price: float,
        lot_size: int,
        atr: float,
        equity: float,
    ) -> int:
        """Return optimal number of lots for the given risk parameters.

        Returns at least 1 lot even when the model suggests less.
        """
        if price <= 0 or lot_size <= 0 or atr <= 0 or equity <= 0:
            return 1

        risk_budget = equity * self.risk_per_trade_pct
        risk_per_share = atr * self.sl_atr_mult
        shares = risk_budget / risk_per_share
        lots = int(shares / lot_size)
        return max(1, lots)
