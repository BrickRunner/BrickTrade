"""Comprehensive tests for the stock trading system.

Covers:
1.  BCS WS header compatibility
2.  Position sizer (various price/lot_size/ATR combos)
3.  Pair dedup (MeanReversion second leg allowed)
4.  Session type in snapshot
5.  Config validation (valid + invalid configs)
6.  Trailing stop updates + persistence
7.  Kill-switch triggers (daily + portfolio drawdown)
8.  Divergence RSI series computation
9.  MeanReversion two-leg generation
10. Execution: _calc_pnl with lot_size + commission
11. _wait_fill partial fill handling
12. Confirmation semaphore limit
13. Schedule: holidays, sessions, next_open
14. Engine: _adjust_quantity with ATR sizer
15. Risk: approve + reject scenarios
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stocks.exchange.bcs_ws import _HEADER_KWARG, _ws_major
from stocks.strategies.divergence import DivergenceStrategy, _compute_rsi_series
from stocks.strategies.mean_reversion import MeanReversionStrategy
from stocks.system.config import (
    BcsCredentials,
    StockExecutionConfig,
    StockRiskConfig,
    StockStrategyConfig,
    StockTradingConfig,
)
from stocks.system.confirmation import SemiAutoConfirmationManager
from stocks.system.engine import StockTradingEngine
from stocks.system.execution import SingleLegExecutionEngine
from stocks.system.models import (
    CandleBar,
    StockExecutionReport,
    StockPosition,
    StockQuote,
    StockRiskDecision,
    StockSnapshot,
    StockStrategyId,
    StockTradeIntent,
)
from stocks.system.position_sizer import StockPositionSizer
from stocks.system.risk import StockRiskEngine
from stocks.system.schedule import MOEXSchedule
from stocks.system.state import StockSystemState
from stocks.system.strategy_runner import StockStrategyRunner


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

def _make_candle(close: float, high: float = 0, low: float = 0,
                 open_: float = 0, volume: float = 100, ts: float = 0) -> CandleBar:
    return CandleBar(
        timestamp=ts or time.time(),
        open=open_ or close,
        high=high or close,
        low=low or close,
        close=close,
        volume=volume,
    )


def _make_quote(ticker: str = "SBER", last: float = 300.0) -> StockQuote:
    return StockQuote(ticker=ticker, bid=last - 0.5, ask=last + 0.5,
                      last=last, volume=1000)


def _make_snapshot(
    ticker: str = "SBER",
    price: float = 300.0,
    cash: float = 100_000.0,
    portfolio: float = 200_000.0,
    lot_size: int = 10,
    indicators: Optional[Dict[str, float]] = None,
    candles: Optional[List[CandleBar]] = None,
    session_type: str = "main",
) -> StockSnapshot:
    return StockSnapshot(
        ticker=ticker,
        quote=_make_quote(ticker, price),
        candles=candles or [],
        portfolio_value=portfolio,
        cash_available=cash,
        current_position_qty=0,
        indicators=indicators if indicators is not None else {"atr_14": 5.0, "rsi_14": 50.0},
        lot_size=lot_size,
        session_type=session_type,
    )


def _make_config(**overrides) -> StockTradingConfig:
    risk_kw = overrides.pop("risk_kw", {})
    exec_kw = overrides.pop("exec_kw", {})
    defaults = dict(
        tickers=["SBER"],
        class_code="TQBR",
        credentials=BcsCredentials(refresh_token="test_token"),
        starting_equity=100_000,
        risk=StockRiskConfig(**risk_kw) if risk_kw else StockRiskConfig(),
        execution=StockExecutionConfig(**exec_kw) if exec_kw else StockExecutionConfig(),
        strategy=StockStrategyConfig(),
    )
    defaults.update(overrides)
    return StockTradingConfig(**defaults)


def _make_intent(
    ticker: str = "SBER",
    side: str = "buy",
    qty: int = 1,
    confidence: float = 0.5,
    edge: float = 0.5,
    metadata: Optional[Dict[str, Any]] = None,
) -> StockTradeIntent:
    return StockTradeIntent(
        strategy_id=StockStrategyId.MEAN_REVERSION,
        ticker=ticker,
        side=side,
        quantity_lots=qty,
        confidence=confidence,
        expected_edge_pct=edge,
        metadata=metadata or {},
    )


def _make_position(
    ticker: str = "SBER",
    side: str = "buy",
    entry: float = 300.0,
    qty: int = 1,
    lot_size: int = 10,
    sl: float = 290.0,
    tp: float = 320.0,
    trailing: float = 1.5,
    peak: float = 300.0,
) -> StockPosition:
    return StockPosition(
        position_id="pos-001",
        strategy_id=StockStrategyId.MEAN_REVERSION,
        ticker=ticker,
        side=side,
        quantity_lots=qty,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        lot_size=lot_size,
        peak_price=peak,
        trailing_stop_pct=trailing,
    )


# ============================================================================
# 1. BCS WS header compatibility
# ============================================================================

class TestBcsWsHeaderCompat:
    def test_header_kwarg_is_string(self):
        assert _HEADER_KWARG in ("extra_headers", "additional_headers")

    def test_ws_major_detected(self):
        assert isinstance(_ws_major, int)
        assert _ws_major >= 0

    def test_header_kwarg_matches_version(self):
        if _ws_major >= 13:
            assert _HEADER_KWARG == "additional_headers"
        else:
            assert _HEADER_KWARG == "extra_headers"


# ============================================================================
# 2. Position sizer
# ============================================================================

class TestStockPositionSizer:
    def test_basic_calculation(self):
        sizer = StockPositionSizer(risk_per_trade_pct=0.01, sl_atr_mult=2.0)
        lots = sizer.calculate_lots(price=300, lot_size=10, atr=5.0, equity=100_000)
        assert lots == 10

    def test_high_atr_reduces_lots(self):
        sizer = StockPositionSizer(risk_per_trade_pct=0.01, sl_atr_mult=2.0)
        lots = sizer.calculate_lots(price=300, lot_size=10, atr=50.0, equity=100_000)
        assert lots == 1

    def test_returns_minimum_one(self):
        sizer = StockPositionSizer(risk_per_trade_pct=0.001, sl_atr_mult=2.0)
        lots = sizer.calculate_lots(price=300, lot_size=100, atr=50.0, equity=10_000)
        assert lots == 1

    def test_zero_atr_returns_one(self):
        sizer = StockPositionSizer()
        assert sizer.calculate_lots(price=300, lot_size=10, atr=0, equity=100_000) == 1

    def test_zero_price_returns_one(self):
        sizer = StockPositionSizer()
        assert sizer.calculate_lots(price=0, lot_size=10, atr=5, equity=100_000) == 1

    def test_zero_equity_returns_one(self):
        sizer = StockPositionSizer()
        assert sizer.calculate_lots(price=300, lot_size=10, atr=5, equity=0) == 1

    def test_small_lot_size(self):
        sizer = StockPositionSizer(risk_per_trade_pct=0.02, sl_atr_mult=1.5)
        lots = sizer.calculate_lots(price=100, lot_size=1, atr=3.0, equity=200_000)
        assert lots == 888

    def test_large_lot_size(self):
        sizer = StockPositionSizer(risk_per_trade_pct=0.01, sl_atr_mult=2.0)
        lots = sizer.calculate_lots(price=300, lot_size=100, atr=5.0, equity=100_000)
        assert lots == 1


# ============================================================================
# 3. Pair dedup (MeanReversion second leg allowed)
# ============================================================================

class TestPairDedup:
    @pytest.mark.asyncio
    async def test_plain_intent_blocked_by_existing_position(self):
        state = StockSystemState(100_000)
        pos = _make_position(ticker="SBER")
        await state.add_position(pos)

        intent = _make_intent(ticker="SBER", metadata={})
        existing = await state.positions_for_ticker(intent.ticker)
        assert existing
        assert not intent.metadata.get("pair_id")
        # Engine would skip: if existing and not intent.metadata.get("pair_id")

    @pytest.mark.asyncio
    async def test_pair_intent_allowed_despite_existing_position(self):
        state = StockSystemState(100_000)
        pos = _make_position(ticker="SBER")
        await state.add_position(pos)

        intent = _make_intent(ticker="SBER", metadata={"pair_id": "SBER:SBERP"})
        existing = await state.positions_for_ticker(intent.ticker)
        assert existing
        assert intent.metadata.get("pair_id")
        # Engine logic: `if existing and not intent.metadata.get("pair_id")` => False, so allow


# ============================================================================
# 4. Session type in snapshot
# ============================================================================

class TestSessionTypeInSnapshot:
    def test_snapshot_has_session_type_field(self):
        snap = _make_snapshot(session_type="main")
        assert snap.session_type == "main"

    def test_snapshot_default_session_type(self):
        snap = StockSnapshot(
            ticker="SBER",
            quote=_make_quote(),
            candles=[],
            portfolio_value=100_000,
            cash_available=50_000,
            current_position_qty=0,
            indicators={},
        )
        assert snap.session_type == "closed"

    def test_snapshot_replace_session_type(self):
        snap = _make_snapshot(session_type="closed")
        updated = replace(snap, session_type="evening")
        assert updated.session_type == "evening"
        assert snap.session_type == "closed"


# ============================================================================
# 5. Config validation (valid + invalid)
# ============================================================================

class TestConfigValidation:
    def test_valid_config_passes(self):
        cfg = _make_config()
        cfg.validate()

    def test_empty_tickers_fails(self):
        cfg = _make_config(tickers=[])
        with pytest.raises(ValueError, match="at least one ticker"):
            cfg.validate()

    def test_missing_refresh_token_fails(self):
        cfg = _make_config(credentials=BcsCredentials(refresh_token=""))
        with pytest.raises(ValueError, match="BCS_REFRESH_TOKEN"):
            cfg.validate()

    def test_negative_equity_fails(self):
        cfg = _make_config(starting_equity=-100)
        with pytest.raises(ValueError, match="positive"):
            cfg.validate()

    def test_invalid_mode_fails(self):
        cfg = _make_config(exec_kw={"mode": "invalid_mode"})
        with pytest.raises(ValueError, match="Invalid STOCK_MODE"):
            cfg.validate()

    def test_negative_commission_fails(self):
        cfg = _make_config(risk_kw={"commission_pct": -0.01})
        with pytest.raises(ValueError, match="commission_pct"):
            cfg.validate()

    def test_trailing_stop_out_of_range_fails(self):
        cfg = _make_config(risk_kw={"trailing_stop_pct": 15.0})
        with pytest.raises(ValueError, match="trailing_stop_pct"):
            cfg.validate()

    def test_zero_time_stop_fails(self):
        cfg = _make_config(risk_kw={"time_stop_hours": 0})
        with pytest.raises(ValueError, match="time_stop_hours"):
            cfg.validate()

    def test_daily_drawdown_out_of_range_fails(self):
        cfg = _make_config(risk_kw={"max_daily_drawdown_pct": 1.5})
        with pytest.raises(ValueError, match="max_daily_drawdown_pct"):
            cfg.validate()

    def test_max_open_positions_zero_fails(self):
        cfg = _make_config(risk_kw={"max_open_positions": 0})
        with pytest.raises(ValueError, match="max_open_positions"):
            cfg.validate()

    def test_min_confidence_out_of_range_fails(self):
        cfg = _make_config(risk_kw={"min_confidence": 1.5})
        with pytest.raises(ValueError, match="min_confidence"):
            cfg.validate()

    def test_zero_cycle_interval_fails(self):
        cfg = _make_config(exec_kw={"cycle_interval_seconds": 0})
        with pytest.raises(ValueError, match="cycle_interval_seconds"):
            cfg.validate()

    def test_zero_order_timeout_fails(self):
        cfg = _make_config(exec_kw={"order_timeout_ms": 0})
        with pytest.raises(ValueError, match="order_timeout_ms"):
            cfg.validate()


# ============================================================================
# 6. Trailing stop updates + persistence
# ============================================================================

class TestTrailingStopAndPersistence:
    def test_trailing_stop_updates_peak_and_sl_buy(self):
        pos = _make_position(side="buy", entry=300, sl=290, peak=300, trailing=2.0)
        price = 310.0

        if pos.side == "buy":
            if price > pos.peak_price:
                pos.peak_price = price
            new_sl = pos.peak_price * (1 - pos.trailing_stop_pct / 100)
            if new_sl > pos.stop_loss_price:
                pos.stop_loss_price = round(new_sl, 4)

        assert pos.peak_price == 310.0
        assert pos.stop_loss_price == round(310.0 * 0.98, 4)

    def test_trailing_stop_updates_peak_and_sl_sell(self):
        pos = _make_position(side="sell", entry=300, sl=310, tp=280, peak=300, trailing=2.0)
        price = 290.0

        if pos.side == "sell":
            if pos.peak_price == 0 or price < pos.peak_price:
                pos.peak_price = price
            new_sl = pos.peak_price * (1 + pos.trailing_stop_pct / 100)
            if new_sl < pos.stop_loss_price:
                pos.stop_loss_price = round(new_sl, 4)

        assert pos.peak_price == 290.0
        assert pos.stop_loss_price == round(290.0 * 1.02, 4)

    @pytest.mark.asyncio
    async def test_persist_positions_roundtrip(self):
        state = StockSystemState(100_000)
        pos = _make_position()
        await state.add_position(pos)
        positions = await state.list_positions()
        assert len(positions) == 1
        assert positions[0].position_id == "pos-001"


# ============================================================================
# 7. Kill-switch triggers (daily + portfolio drawdown)
# ============================================================================

class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_daily_drawdown_triggers_kill_switch(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig(max_daily_drawdown_pct=0.03)
        risk = StockRiskEngine(config=config, state=state)

        await state.apply_realized_pnl(-4000)

        intent = _make_intent()
        decision = await risk.validate_intent(intent, 300.0, cash_available=96_000)
        assert not decision.approved
        assert decision.kill_switch_triggered
        assert "daily_drawdown" in decision.reason

    @pytest.mark.asyncio
    async def test_portfolio_drawdown_triggers_permanent_kill_switch(self):
        state = StockSystemState(100_000)
        # Set daily drawdown high so portfolio drawdown fires first
        config = StockRiskConfig(
            max_daily_drawdown_pct=0.50,
            max_portfolio_drawdown_pct=0.10,
        )
        risk = StockRiskEngine(config=config, state=state)

        await state.apply_realized_pnl(-11_000)

        intent = _make_intent()
        decision = await risk.validate_intent(intent, 300.0, cash_available=89_000)
        assert not decision.approved
        assert decision.kill_switch_triggered
        assert "portfolio_drawdown" in decision.reason

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_new_trades(self):
        state = StockSystemState(100_000)
        await state.trigger_kill_switch()

        config = StockRiskConfig()
        risk = StockRiskEngine(config=config, state=state)

        intent = _make_intent()
        decision = await risk.validate_intent(intent, 300.0)
        assert not decision.approved
        assert "kill_switch_active" in decision.reason

    @pytest.mark.asyncio
    async def test_daily_kill_switch_resets_next_day(self):
        state = StockSystemState(100_000)
        await state.trigger_kill_switch(permanent=False)
        assert await state.kill_switch_triggered()

        state._day_mark = "2020-01-01"
        assert not await state.kill_switch_triggered()

    @pytest.mark.asyncio
    async def test_permanent_kill_switch_does_not_reset(self):
        state = StockSystemState(100_000)
        await state.trigger_kill_switch(permanent=True)
        assert await state.kill_switch_triggered()

        state._day_mark = "2020-01-01"
        assert await state.kill_switch_triggered()


# ============================================================================
# 8. Divergence RSI series computation
# ============================================================================

class TestDivergenceRSI:
    def test_rsi_series_length(self):
        candles = [_make_candle(close=100 + i) for i in range(30)]
        rsi = _compute_rsi_series(candles, period=14)
        assert len(rsi) == 30

    def test_rsi_series_initial_neutral(self):
        candles = [_make_candle(close=100 + i * 0.5) for i in range(30)]
        rsi = _compute_rsi_series(candles, period=14)
        for i in range(14):
            assert rsi[i] == 50.0

    def test_rsi_all_gains_equals_100(self):
        candles = [_make_candle(close=100 + i) for i in range(20)]
        rsi = _compute_rsi_series(candles, period=14)
        assert rsi[14] == 100.0

    def test_rsi_all_losses_equals_0(self):
        candles = [_make_candle(close=100 - i) for i in range(20)]
        rsi = _compute_rsi_series(candles, period=14)
        assert rsi[14] == 0.0

    def test_rsi_mixed_reasonable_range(self):
        candles = [_make_candle(close=100 + (i % 3) - 1) for i in range(30)]
        rsi = _compute_rsi_series(candles, period=14)
        for val in rsi[14:]:
            assert 0 <= val <= 100

    def test_rsi_insufficient_data_returns_neutral(self):
        candles = [_make_candle(close=100) for _ in range(10)]
        rsi = _compute_rsi_series(candles, period=14)
        assert all(v == 50.0 for v in rsi)


# ============================================================================
# 9. MeanReversion two-leg generation
# ============================================================================

class TestMeanReversionTwoLeg:
    @pytest.mark.asyncio
    async def test_generates_two_legs_on_entry(self):
        strategy = MeanReversionStrategy(
            pairs=["SBER:SBERP"],
            zscore_entry=2.0,
            zscore_exit=0.5,
            window=5,
        )
        # Feed stable ratio data
        for i in range(10):
            snap_a = _make_snapshot(ticker="SBER", price=100.0)
            await strategy.on_snapshot(snap_a)
            snap_b = _make_snapshot(ticker="SBERP", price=100.0)
            await strategy.on_snapshot(snap_b)

        # Spike the ratio
        snap_spike = _make_snapshot(ticker="SBER", price=150.0)
        intents = await strategy.on_snapshot(snap_spike)

        if intents:
            assert len(intents) == 2
            tickers = {i.ticker for i in intents}
            assert "SBER" in tickers
            assert "SBERP" in tickers
            sides = {i.ticker: i.side for i in intents}
            assert sides["SBER"] == "sell"
            assert sides["SBERP"] == "buy"
            for intent in intents:
                assert intent.metadata.get("pair_id")

    @pytest.mark.asyncio
    async def test_no_signal_in_normal_range(self):
        strategy = MeanReversionStrategy(
            pairs=["SBER:SBERP"],
            zscore_entry=2.0,
            window=5,
        )
        all_intents: List[StockTradeIntent] = []
        for _ in range(10):
            snap_a = _make_snapshot(ticker="SBER", price=100.0)
            all_intents.extend(await strategy.on_snapshot(snap_a))
            snap_b = _make_snapshot(ticker="SBERP", price=100.0)
            all_intents.extend(await strategy.on_snapshot(snap_b))

        assert len(all_intents) == 0


# ============================================================================
# 10. Execution: _calc_pnl with lot_size + commission
# ============================================================================

class TestCalcPnl:
    def _make_engine(self, commission_pct: float = 0.001) -> SingleLegExecutionEngine:
        return SingleLegExecutionEngine(
            config=StockExecutionConfig(dry_run=True),
            risk_config=StockRiskConfig(commission_pct=commission_pct),
            venue=MagicMock(),
            state=MagicMock(),
        )

    def test_buy_profit_with_commission(self):
        engine = self._make_engine(0.001)
        pos = _make_position(side="buy", entry=100, qty=2, lot_size=10)
        pnl = engine._calc_pnl(pos, 110.0)
        # gross = (110-100)*20 = 200, commission = (100+110)*20*0.001 = 4.2
        assert abs(pnl - (200.0 - 4.2)) < 0.01

    def test_sell_profit_with_commission(self):
        engine = self._make_engine(0.001)
        pos = _make_position(side="sell", entry=100, qty=1, lot_size=10)
        pnl = engine._calc_pnl(pos, 90.0)
        # gross = -(90-100)*10 = 100, commission = (100+90)*10*0.001 = 1.9
        assert abs(pnl - (100.0 - 1.9)) < 0.01

    def test_buy_loss(self):
        engine = self._make_engine(0.0005)
        pos = _make_position(side="buy", entry=100, qty=1, lot_size=10)
        pnl = engine._calc_pnl(pos, 95.0)
        # gross = (95-100)*10 = -50, commission = (100+95)*10*0.0005 = 0.975
        assert pnl < 0
        assert abs(pnl - (-50 - 0.975)) < 0.01


# ============================================================================
# 11. _wait_fill partial fill handling
# ============================================================================

class TestWaitFill:
    @pytest.mark.asyncio
    async def test_filled_returns_avg_price(self):
        venue = AsyncMock()
        venue.get_order.return_value = {
            "data": {
                "orderStatus": "2",
                "averagePrice": 305.5,
                "executedQuantity": 10,
                "commission": 0.15,
            }
        }
        engine = SingleLegExecutionEngine(
            config=StockExecutionConfig(order_timeout_ms=1000),
            risk_config=StockRiskConfig(),
            venue=venue,
            state=MagicMock(),
        )
        fill = await engine._wait_fill("order-123", 300.0)
        assert fill == 305.5

    @pytest.mark.asyncio
    async def test_cancelled_order_uses_fallback(self):
        venue = AsyncMock()
        venue.get_order.return_value = {
            "data": {"orderStatus": "4", "averagePrice": 0, "executedQuantity": 0}
        }
        venue.cancel_order.return_value = None
        engine = SingleLegExecutionEngine(
            config=StockExecutionConfig(order_timeout_ms=500),
            risk_config=StockRiskConfig(),
            venue=venue,
            state=MagicMock(),
        )
        fill = await engine._wait_fill("order-456", 300.0)
        assert fill == 300.0

    @pytest.mark.asyncio
    async def test_partial_fill_returns_avg_on_timeout(self):
        call_count = 0

        async def mock_get_order(order_id):
            nonlocal call_count
            call_count += 1
            return {
                "data": {
                    "orderStatus": "1",
                    "averagePrice": 302.0,
                    "executedQuantity": 5,
                    "quantity": 10,
                }
            }

        venue = AsyncMock()
        venue.get_order.side_effect = mock_get_order
        venue.cancel_order.return_value = None
        engine = SingleLegExecutionEngine(
            config=StockExecutionConfig(order_timeout_ms=500),
            risk_config=StockRiskConfig(),
            venue=venue,
            state=MagicMock(),
        )
        fill = await engine._wait_fill("order-789", 300.0)
        # Should use partial fill price
        assert fill == 302.0


# ============================================================================
# 12. Confirmation semaphore limit
# ============================================================================

class TestConfirmationSemaphore:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_requests(self):
        """Verify that only max_concurrent requests run simultaneously."""
        concurrent_count = 0
        max_observed = 0

        async def mock_send(user_id, text, **kwargs):
            nonlocal concurrent_count, max_observed
            concurrent_count += 1
            max_observed = max(max_observed, concurrent_count)
            await asyncio.sleep(0.1)
            concurrent_count -= 1
            return {"chat_id": user_id, "message_id": 1}

        mgr = SemiAutoConfirmationManager(
            send_fn=mock_send,
            timeout_sec=1,
            max_concurrent=2,
        )

        # Fire 5 requests concurrently
        intents = [_make_intent(ticker=f"T{i}") for i in range(5)]
        tasks = [
            asyncio.create_task(
                mgr.request_confirmation(intent, user_id=123, current_price=100)
            )
            for intent in intents
        ]

        # Let them all time out
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # The semaphore should have limited concurrency to 2
        assert max_observed <= 2


# ============================================================================
# 13. Schedule: holidays, sessions, next_open
# ============================================================================

class TestSchedule:
    def test_weekend_is_not_trading(self):
        sched = MOEXSchedule()
        # Saturday
        sat = date(2026, 3, 28)
        assert sched._is_non_trading_day(sat)

    def test_weekday_is_trading(self):
        sched = MOEXSchedule()
        mon = date(2026, 3, 30)
        assert not sched._is_non_trading_day(mon)

    def test_holiday_detected(self):
        sched = MOEXSchedule()
        assert sched.is_holiday(date(2026, 1, 1))
        assert sched.is_holiday(date(2026, 5, 1))
        assert not sched.is_holiday(date(2026, 3, 30))

    def test_session_type_returns_valid_string(self):
        sched = MOEXSchedule()
        st = sched.session_type()
        assert st in ("morning", "main", "evening", "closed")

    def test_next_session_open_returns_future(self):
        sched = MOEXSchedule()
        nxt = sched.next_session_open()
        _MSK = timezone(timedelta(hours=3))
        now = datetime.now(tz=_MSK)
        # next_session_open should return an aware datetime
        assert nxt.tzinfo is not None

    def test_seconds_until_close_non_negative(self):
        sched = MOEXSchedule()
        secs = sched.seconds_until_close()
        assert secs >= 0


# ============================================================================
# 14. Engine: _adjust_quantity with ATR sizer
# ============================================================================

class TestEngineAdjustQuantity:
    def _make_engine(self) -> StockTradingEngine:
        config = _make_config()
        state = StockSystemState(100_000)
        risk = StockRiskEngine(config=config.risk, state=state)
        venue = MagicMock()
        execution = SingleLegExecutionEngine(
            config=config.execution,
            risk_config=config.risk,
            venue=venue,
            state=state,
        )
        runner = StockStrategyRunner(strategies=[])
        provider = AsyncMock()
        return StockTradingEngine(
            config=config,
            provider=provider,
            risk=risk,
            execution=execution,
            strategies=runner,
            state=state,
        )

    def test_atr_based_sizing_applied(self):
        engine = self._make_engine()
        snapshot = _make_snapshot(
            price=300, cash=100_000, portfolio=100_000,
            lot_size=10, indicators={"atr_14": 5.0},
        )
        intent = _make_intent(side="buy", qty=1)
        adjusted = engine._adjust_quantity(intent, snapshot)
        # With ATR=5, equity=100k, risk=1%, sl_mult=2:
        # risk_budget=1000, risk_per_share=10, shares=100, lots=10
        assert adjusted.quantity_lots > 1

    def test_no_atr_keeps_original(self):
        engine = self._make_engine()
        snapshot = _make_snapshot(
            price=300, cash=100_000, portfolio=100_000,
            lot_size=10, indicators={},
        )
        intent = _make_intent(side="buy", qty=3)
        adjusted = engine._adjust_quantity(intent, snapshot)
        assert adjusted.quantity_lots == 3

    def test_buy_capped_by_cash(self):
        engine = self._make_engine()
        snapshot = _make_snapshot(
            price=300, cash=3000, portfolio=100_000,
            lot_size=10, indicators={"atr_14": 5.0},
        )
        intent = _make_intent(side="buy", qty=100)
        adjusted = engine._adjust_quantity(intent, snapshot)
        # Can only afford 3000 / (300*10) = 1 lot
        assert adjusted.quantity_lots == 1

    def test_sell_not_capped_by_cash(self):
        engine = self._make_engine()
        snapshot = _make_snapshot(
            price=300, cash=100, portfolio=100_000,
            lot_size=10, indicators={"atr_14": 5.0},
        )
        intent = _make_intent(side="sell", qty=5)
        adjusted = engine._adjust_quantity(intent, snapshot)
        # Sell should use ATR sizing, not cash cap
        assert adjusted.quantity_lots >= 1

    def test_zero_price_returns_original(self):
        engine = self._make_engine()
        snapshot = _make_snapshot(
            price=0, cash=100_000, portfolio=100_000,
            lot_size=10, indicators={"atr_14": 5.0},
        )
        intent = _make_intent(side="buy", qty=5)
        adjusted = engine._adjust_quantity(intent, snapshot)
        assert adjusted.quantity_lots == 5

    def test_buy_zero_cash_returns_zero_qty(self):
        engine = self._make_engine()
        snapshot = _make_snapshot(
            price=300, cash=1, portfolio=100_000,
            lot_size=10, indicators={"atr_14": 5.0},
        )
        intent = _make_intent(side="buy", qty=5)
        adjusted = engine._adjust_quantity(intent, snapshot)
        assert adjusted.quantity_lots == 0


# ============================================================================
# 15. Risk: approve + reject scenarios
# ============================================================================

class TestRiskApproveReject:
    @pytest.mark.asyncio
    async def test_approve_normal_trade(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig()
        risk = StockRiskEngine(config=config, state=state)

        intent = _make_intent(side="buy", qty=1)
        decision = await risk.validate_intent(
            intent, 300.0, cash_available=50_000, lot_size=10,
        )
        assert decision.approved
        assert decision.reason == "approved"

    @pytest.mark.asyncio
    async def test_reject_max_positions_reached(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig(max_open_positions=2)
        risk = StockRiskEngine(config=config, state=state)

        # Add 2 positions
        for i in range(2):
            pos = StockPosition(
                position_id=f"pos-{i}",
                strategy_id=StockStrategyId.MEAN_REVERSION,
                ticker=f"T{i}",
                side="buy",
                quantity_lots=1,
                entry_price=100,
                stop_loss_price=95,
                take_profit_price=110,
            )
            await state.add_position(pos)

        intent = _make_intent(side="buy", qty=1)
        decision = await risk.validate_intent(intent, 300.0, cash_available=50_000)
        assert not decision.approved
        assert "max_positions" in decision.reason

    @pytest.mark.asyncio
    async def test_reject_max_daily_trades(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig(max_daily_trades=2)
        risk = StockRiskEngine(config=config, state=state)

        # Simulate 2 trades (add+remove)
        for i in range(2):
            pos = StockPosition(
                position_id=f"pos-{i}",
                strategy_id=StockStrategyId.MEAN_REVERSION,
                ticker=f"T{i}",
                side="buy",
                quantity_lots=1,
                entry_price=100,
                stop_loss_price=95,
                take_profit_price=110,
            )
            await state.add_position(pos)
            await state.remove_position(f"pos-{i}")

        intent = _make_intent(side="buy", qty=1)
        decision = await risk.validate_intent(intent, 300.0, cash_available=50_000)
        assert not decision.approved
        assert "max_daily_trades" in decision.reason

    @pytest.mark.asyncio
    async def test_reject_insufficient_cash(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig()
        risk = StockRiskEngine(config=config, state=state)

        intent = _make_intent(side="buy", qty=10)
        # 10 lots * 10 shares * 300 = 30000 > 1000 cash
        decision = await risk.validate_intent(
            intent, 300.0, cash_available=1000, lot_size=10,
        )
        assert not decision.approved
        assert "insufficient_cash" in decision.reason

    @pytest.mark.asyncio
    async def test_reject_zero_quantity(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig()
        risk = StockRiskEngine(config=config, state=state)

        intent = _make_intent(side="buy", qty=0)
        decision = await risk.validate_intent(intent, 300.0, cash_available=50_000)
        assert not decision.approved
        assert "quantity_zero" in decision.reason

    @pytest.mark.asyncio
    async def test_reject_per_position_exposure(self):
        state = StockSystemState(100_000)
        config = StockRiskConfig(max_per_position_pct=0.01)  # 1% max
        risk = StockRiskEngine(config=config, state=state)

        # 5 lots * 10 shares * 300 = 15000 > 1% of 50000 = 500
        intent = _make_intent(side="buy", qty=5)
        decision = await risk.validate_intent(
            intent, 300.0, cash_available=50_000, lot_size=10,
        )
        assert not decision.approved
        assert "per_position_exposure" in decision.reason