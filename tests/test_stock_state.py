"""State tests."""
import os, pytest
from stocks.system.models import StockPosition, StockStrategyId
from stocks.system.state import StockSystemState


def _pos(tid="p1", lot=10, pr=300.0, qty=2):
    return StockPosition(
        position_id=tid,
        strategy_id=StockStrategyId.TREND_FOLLOWING,
        ticker="SBER", side="buy",
        quantity_lots=qty,
        entry_price=pr,
        stop_loss_price=pr*0.97,
        take_profit_price=pr*1.04,
        lot_size=lot,
    )


class TestExposure:
    async def test_lot_size(self):
        s = StockSystemState(starting_equity=500000.0)
        s._positions["p1"] = _pos(lot=10, pr=300.0, qty=2)
        snap = await s.snapshot()
        assert snap["total_exposure"] == 6000.0
