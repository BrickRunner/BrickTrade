"""Tests for stock trading models."""
import pytest
from dataclasses import replace
from stocks.system.models import (
    CandleBar, StockPosition, StockQuote,
    StockSnapshot, StockStrategyId, StockTradeIntent,
)


def _pos(lot_size=10):
    return StockPosition(
        position_id="p1",
        strategy_id=StockStrategyId.TREND_FOLLOWING,
        ticker="SBER", side="buy", quantity_lots=2,
        entry_price=300.0, stop_loss_price=291.0,
        take_profit_price=313.5, lot_size=lot_size,
    )


class TestStockPosition:
    def test_lot_size_default_is_1(self):
        p = StockPosition(
            position_id="x",
            strategy_id=StockStrategyId.BREAKOUT,
            ticker="T", side="buy", quantity_lots=1,
            entry_price=100.0, stop_loss_price=97.0,
            take_profit_price=104.5,
        )
        assert p.lot_size == 1

    def test_lot_size_sber(self):
        assert _pos(10).lot_size == 10

    def test_lot_size_vtbr(self):
        assert _pos(10000).lot_size == 10000


class TestStockTradeIntent:
    def test_frozen(self):
        i = StockTradeIntent(
            strategy_id=StockStrategyId.BREAKOUT,
            ticker="SBER", side="buy", quantity_lots=5,
        )
        with pytest.raises(AttributeError):
            i.ticker = "VTBR"

    def test_replace(self):
        i = StockTradeIntent(
            strategy_id=StockStrategyId.BREAKOUT,
            ticker="SBER", side="buy", quantity_lots=5,
        )
        j = replace(i, quantity_lots=10)
        assert j.quantity_lots == 10
        assert j.ticker == "SBER"
        assert i.quantity_lots == 5

    def test_defaults(self):
        i = StockTradeIntent(
            strategy_id=StockStrategyId.RSI_REVERSAL,
            ticker="GAZP", side="sell", quantity_lots=3,
        )
        assert i.order_type == "market"
        assert i.confidence == 0.0
        assert i.mode == "auto"


class TestStockSnapshot:
    def test_lot_size_field(self):
        q = StockQuote(
            ticker="SBER", bid=299.9, ask=300.1,
            last=300.0, volume=1000.0,
        )
        s = StockSnapshot(
            ticker="SBER", quote=q, candles=[],
            portfolio_value=500000.0, cash_available=100000.0,
            current_position_qty=0, indicators={},
            lot_size=10,
        )
        assert s.lot_size == 10
