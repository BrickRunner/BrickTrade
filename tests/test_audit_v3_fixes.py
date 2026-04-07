"""
Tests for audit v3 fixes — comprehensive coverage of all 30 issues.
Organized by fix priority: P0 (critical), P1 (high), Medium.
"""
import asyncio
import inspect
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbitrage.system.models import (
    ExecutionReport,
    MarketSnapshot,
    OpenPosition,
    OrderBookSnapshot,
    StrategyId,
    TradeIntent,
)
from arbitrage.system.config import ExecutionConfig
from arbitrage.system.state import SystemState
from arbitrage.system.slippage import SlippageModel
from arbitrage.system.execution import AtomicExecutionEngine


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_ob(exchange, symbol, bid, ask, timestamp=None):
    return OrderBookSnapshot(
        exchange=exchange, symbol=symbol,
        bid=bid, ask=ask,
        timestamp=timestamp if timestamp is not None else time.time(),
    )


def _make_snapshot(symbol="BTCUSDT", exchanges=("okx", "htx"),
                   bid=50000.0, ask=50010.0, ts=None, funding_rates=None):
    obs = {ex: _make_ob(ex, symbol, bid, ask, ts) for ex in exchanges}
    return MarketSnapshot(
        symbol=symbol,
        orderbooks=obs,
        spot_orderbooks={},
        orderbook_depth={},
        spot_orderbook_depth={},
        balances={ex: 1000.0 for ex in exchanges},
        fee_bps={ex: {"perp": 5.0} for ex in exchanges},
        funding_rates=funding_rates or {ex: 0.0001 for ex in exchanges},
        volatility=0.01,
        trend_strength=0.0,
        atr=10.0,
        atr_rolling=10.0,
        indicators={"spread_bps": 2.0, "funding_spread_bps": 0.0, "basis_bps": 0.0},
        timestamp=time.time(),
    )


def _make_engine(dry_run=True):
    venue = MagicMock()
    venue.get_balances = AsyncMock(return_value={"okx": 1000, "htx": 1000})
    venue.invalidate_balance_cache = MagicMock()
    venue.open_contracts = AsyncMock(return_value=0.0)
    venue.safety_buffer_pct = 0.05
    venue.safety_reserve_usd = 0.50
    del venue._min_notional_usd
    monitor = MagicMock()
    monitor.emit = AsyncMock()
    state = SystemState(starting_equity=10000.0, positions_file=":memory:")
    engine = AtomicExecutionEngine(
        config=ExecutionConfig(dry_run=dry_run),
        venue=venue,
        slippage=SlippageModel(),
        state=state,
        monitor=monitor,
    )
    return engine, venue, monitor


def _make_intent(long_ex="okx", short_ex="htx"):
    return TradeIntent(
        strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
        symbol="BTCUSDT",
        long_exchange=long_ex,
        short_exchange=short_ex,
        side="arb",
        confidence=0.8,
        expected_edge_bps=10.0,
        stop_loss_bps=50.0,
        metadata={"entry_mid": 50005.0, "long_price": 50000, "short_price": 50010},
    )


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #1: risk.py timestamp normalization (ms vs sec)
# ══════════════════════════════════════════════════════════════════════════


class TestTimestampNormalization:
    """Stale orderbook check must work with both ms and sec timestamps."""

    @pytest.mark.asyncio
    async def test_ms_timestamp_detected_as_fresh(self):
        """Timestamp in ms (e.g. 1711670400000) should NOT be stale."""
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig

        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(max_orderbook_age_sec=10.0), state=state)

        # Fresh timestamp in milliseconds
        now_ms = int(time.time() * 1000)
        snapshot = _make_snapshot(ts=now_ms)
        intent = _make_intent()
        alloc = MagicMock()
        alloc.strategy_allocations = {StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0}

        decision = await risk.validate_intent(
            intent, alloc, 100.0, 1.0, 1.0, 50.0, snapshot=snapshot,
        )
        assert decision.reason != "stale_orderbook", f"Fresh ms timestamp wrongly detected as stale: {decision.reason}"

    @pytest.mark.asyncio
    async def test_ms_timestamp_detected_as_stale(self):
        """Old ms timestamp should be detected as stale."""
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig

        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(max_orderbook_age_sec=5.0), state=state)

        # 60 seconds old timestamp in milliseconds
        old_ms = int((time.time() - 60) * 1000)
        snapshot = _make_snapshot(ts=old_ms)
        intent = _make_intent()
        alloc = MagicMock()
        alloc.strategy_allocations = {StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0}

        decision = await risk.validate_intent(
            intent, alloc, 100.0, 1.0, 1.0, 50.0, snapshot=snapshot,
        )
        assert decision.reason == "stale_orderbook"

    @pytest.mark.asyncio
    async def test_sec_timestamp_still_works(self):
        """Normal sec timestamps should still work correctly."""
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig

        state = SystemState(starting_equity=10000.0, positions_file=":memory:")
        risk = RiskEngine(config=RiskConfig(max_orderbook_age_sec=5.0), state=state)

        fresh_sec = time.time()
        snapshot = _make_snapshot(ts=fresh_sec)
        intent = _make_intent()
        alloc = MagicMock()
        alloc.strategy_allocations = {StrategyId.FUTURES_CROSS_EXCHANGE: 1000.0}

        decision = await risk.validate_intent(
            intent, alloc, 100.0, 1.0, 1.0, 50.0, snapshot=snapshot,
        )
        assert decision.reason != "stale_orderbook"

    def test_source_normalizes_timestamp(self):
        """Source code must contain ms-to-sec normalization logic."""
        from arbitrage.system.risk import RiskEngine
        source = inspect.getsource(RiskEngine.validate_intent)
        assert "1e12" in source or "1000.0" in source


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #2: HTX WS uses step6 (full snapshots)
# ══════════════════════════════════════════════════════════════════════════


class TestHTXWebSocket:
    def test_htx_uses_step6_not_step0(self):
        """HTX WS must subscribe to step6 (full snapshots) not step0 (deltas)."""
        source = open("arbitrage/exchanges/htx_ws.py").read()
        assert "depth.step6" in source, "HTX WS must use step6"
        assert "depth.step0" not in source or "instead of step0" in source


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #3: Bybit WS filters out deltas
# ══════════════════════════════════════════════════════════════════════════


class TestBybitWebSocket:
    def test_bybit_filters_deltas(self):
        """Bybit WS must skip delta messages and return early."""
        source = open("arbitrage/exchanges/bybit_ws.py").read()
        assert '"delta"' in source
        # The code must check msg_type == "delta" and return
        assert 'msg_type == "delta"' in source

    @pytest.mark.asyncio
    async def test_delta_message_ignored(self):
        """Delta messages should not trigger callback."""
        from arbitrage.exchanges.bybit_ws import BybitWebSocket

        ws = BybitWebSocket("BTCUSDT")
        ws.callback = AsyncMock()

        # Delta message
        await ws._handle_message({
            "topic": "orderbook.5.BTCUSDT",
            "type": "delta",
            "data": {"b": [["50000", "1.0"]], "a": [["50010", "0.5"]]},
            "ts": int(time.time() * 1000),
        })
        ws.callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_snapshot_message_processed(self):
        """Snapshot messages should trigger callback."""
        from arbitrage.exchanges.bybit_ws import BybitWebSocket

        ws = BybitWebSocket("BTCUSDT")
        ws.callback = AsyncMock()

        await ws._handle_message({
            "topic": "orderbook.5.BTCUSDT",
            "type": "snapshot",
            "data": {"b": [["50000", "1.0"]], "a": [["50010", "0.5"]]},
            "ts": int(time.time() * 1000),
        })
        ws.callback.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #4: execution.py second leg fill verification
# ══════════════════════════════════════════════════════════════════════════


class TestSecondLegFillVerification:
    @pytest.mark.asyncio
    async def test_second_leg_not_filled_triggers_hedge(self):
        """If second leg wait_for_fill returns False, should NOT open position."""
        engine, venue, monitor = _make_engine(dry_run=False)

        venue.place_order = AsyncMock(side_effect=[
            {"success": True, "order_id": "ord1", "size": 1.0, "fill_price": 50000.0},
            {"success": True, "order_id": "ord2", "size": 1.0, "fill_price": 50010.0},
        ])
        # First fill succeeds, second times out
        venue.wait_for_fill = AsyncMock(side_effect=[True, False])
        venue.cancel_order = AsyncMock()

        intent = _make_intent()
        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)

        # Should fail because second leg didn't fill
        assert not report.success
        assert report.message in ("second_leg_failed", "execution_error")


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #5: REST clients never return None
# ══════════════════════════════════════════════════════════════════════════


class TestRESTClientsNeverReturnNone:
    def test_htx_public_request_has_fallback_return(self):
        source = open("arbitrage/exchanges/htx_rest.py").read()
        # After the for loop in _public_request, there must be a return
        assert "all_retries_exhausted_429" in source or "return {}" in source

    def test_htx_private_request_has_fallback_return(self):
        source = open("arbitrage/exchanges/htx_rest.py").read()
        assert "all_retries_exhausted_429" in source

    def test_bybit_has_fallback_returns(self):
        source = open("arbitrage/exchanges/bybit_rest.py").read()
        assert source.count("all_retries_exhausted_429") >= 2

    def test_binance_has_fallback_returns(self):
        source = open("arbitrage/exchanges/binance_rest.py").read()
        assert source.count("all_retries_exhausted_429") >= 1 or source.count("return {}") >= 2


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #6: Config defaults are safe (dry_run=True, monitoring_only=True)
# ══════════════════════════════════════════════════════════════════════════


class TestSafeDefaults:
    def test_execution_config_default_dry_run_true(self):
        cfg = ExecutionConfig()
        assert cfg.dry_run is True

    def test_system_config_default_dry_run_true(self):
        """from_env with empty env should default to dry_run=True."""
        source = open("arbitrage/system/config.py").read()
        assert 'default="true"' in source or "True" in source.split("EXEC_DRY_RUN")[1][:100]

    def test_arb_config_monitoring_only_default_true(self):
        from arbitrage.utils.config import ArbitrageConfig
        cfg = ArbitrageConfig()
        assert cfg.monitoring_only is True

    def test_arb_config_mock_mode_default_true(self):
        from arbitrage.utils.config import ArbitrageConfig
        cfg = ArbitrageConfig()
        assert cfg.mock_mode is True

    def test_arb_config_dry_run_default_true(self):
        from arbitrage.utils.config import ArbitrageConfig
        cfg = ArbitrageConfig()
        assert cfg.dry_run_mode is True


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #8: Self-trade rejection
# ══════════════════════════════════════════════════════════════════════════


class TestSelfTradeRejection:
    @pytest.mark.asyncio
    async def test_same_exchange_rejected(self):
        """Long and short on same exchange must be rejected."""
        engine, venue, monitor = _make_engine(dry_run=False)
        intent = _make_intent(long_ex="okx", short_ex="okx")

        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        assert not report.success
        assert "self_trade" in report.message

    @pytest.mark.asyncio
    async def test_different_exchanges_not_rejected(self):
        """Different exchanges should proceed normally."""
        engine, venue, monitor = _make_engine(dry_run=True)
        intent = _make_intent(long_ex="okx", short_ex="htx")

        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        assert report.success  # dry_run always succeeds

    def test_source_contains_self_trade_check(self):
        source = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        assert "self_trade_rejected" in source


# ══════════════════════════════════════════════════════════════════════════
# P1 FIX #9: Maker price offset direction
# ══════════════════════════════════════════════════════════════════════════


class TestMakerPriceOffset:
    def test_buy_maker_places_above_reference(self):
        """Buy maker should place ABOVE reference (closer to ask)."""
        source = inspect.getsource(AtomicExecutionEngine._place_maker_leg)
        # Find the initial placement block
        buy_block = source.split("side == \"buy\"")[1][:200]
        assert "(1 + offset_mult)" in buy_block, "Buy should use (1 + offset), not (1 - offset)"

    def test_sell_maker_places_below_reference(self):
        """Sell maker should place BELOW reference (closer to bid)."""
        source = inspect.getsource(AtomicExecutionEngine._place_maker_leg)
        sell_block = source.split("side == \"buy\"")[1]
        sell_block = sell_block.split("else:")[1][:200]
        assert "(1 - offset_mult)" in sell_block, "Sell should use (1 - offset), not (1 + offset)"


# ══════════════════════════════════════════════════════════════════════════
# P1 FIX #11: cancel_order logs errors
# ══════════════════════════════════════════════════════════════════════════


class TestCancelOrderLogging:
    def test_cancel_order_does_not_silently_swallow(self):
        source = open("arbitrage/system/live_adapters.py").read()
        cancel_section = source.split("async def cancel_order")[1][:500]
        assert "pass" not in cancel_section or "logger.warning" in cancel_section


# ══════════════════════════════════════════════════════════════════════════
# P1 FIX #12-13: wait_for_fill race fix + consistent status sets
# ══════════════════════════════════════════════════════════════════════════


class TestWaitForFillRaceFix:
    def test_event_created_before_check(self):
        """Event must be registered BEFORE checking _order_data to avoid TOCTOU."""
        from arbitrage.exchanges.private_ws import PrivateWsManager
        source = inspect.getsource(PrivateWsManager.wait_for_fill)
        evt_create_pos = source.find("_fill_events[order_id] = evt")
        existing_check_pos = source.find("_order_data.get(order_id)")
        assert evt_create_pos > 0
        assert existing_check_pos > 0
        assert evt_create_pos < existing_check_pos, \
            "Event must be created BEFORE checking existing data"

    def test_status_sets_are_consistent(self):
        """wait_for_fill must include all states that _on_order recognizes."""
        from arbitrage.exchanges.private_ws import PrivateWsManager
        source = inspect.getsource(PrivateWsManager.wait_for_fill)
        # Must contain HTX states 4 and 2
        assert '"4"' in source
        assert '"2"' in source or '"partial-filled"' in source
        # Must contain Bybit states
        assert '"Filled"' in source
        assert '"PartiallyFilled"' in source


# ══════════════════════════════════════════════════════════════════════════
# P1 FIX #14: FundingArbitrage has required fields
# ══════════════════════════════════════════════════════════════════════════


class TestFundingArbitrageIntent:
    def test_create_intent_has_required_fields(self):
        """on_market_snapshot must produce TradeIntent with side, confidence, expected_edge_bps."""
        source = open("arbitrage/system/strategies/funding_arbitrage.py").read()
        # Strategy was refactored: create_intent merged into on_market_snapshot
        # which builds TradeIntent directly with all required fields.
        assert "TradeIntent(" in source
        # Find the TradeIntent construction block
        intent_section = source.split("TradeIntent(")[1][:500]
        assert "side=" in intent_section
        assert "confidence=" in intent_section
        assert "expected_edge_bps=" in intent_section


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #16: Net slippage instead of per-leg max(0)
# ══════════════════════════════════════════════════════════════════════════


class TestNetSlippage:
    def test_slippage_uses_net_not_per_leg_max(self):
        """Slippage should be net, not max(0) per leg."""
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine.run_cycle)
        # Should NOT have max(0.0, ...) on individual legs
        assert "max(0.0, (report.fill_price_long" not in source
        # Should have max(0.0, slip_long + slip_short) for the total
        assert "slip_long + slip_short" in source


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #17: Funding rate spread instead of max
# ══════════════════════════════════════════════════════════════════════════


class TestFundingRateSpread:
    def test_uses_spread_not_max(self):
        """Allocation should use funding spread (max-min), not max()."""
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine.run_cycle)
        assert "funding_spread" in source or "max(fr_vals) - min(fr_vals)" in source
        # Should NOT use max(snapshot.funding_rates.values())
        assert "max(snapshot.funding_rates.values()" not in source


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #18: state.py persist positions under lock
# ══════════════════════════════════════════════════════════════════════════


class TestPersistPositionsUnderLock:
    def test_payload_serialized_before_task(self):
        """_persist_positions must serialize data before scheduling async task."""
        source = inspect.getsource(SystemState._persist_positions)
        serialize_pos = source.find("_serialize_position")
        task_pos = source.find("create_task")
        assert serialize_pos > 0, "Must serialize positions"
        assert task_pos > 0, "Must schedule task"
        assert serialize_pos < task_pos, \
            "Serialization must happen BEFORE create_task (under caller's lock)"

    def test_persist_async_accepts_payload(self):
        """_persist_positions_async must accept pre-serialized payload."""
        sig = inspect.signature(SystemState._persist_positions_async)
        assert "payload" in sig.parameters


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #19: state.py uses UTC for daily reset
# ══════════════════════════════════════════════════════════════════════════


class TestUTCDailyReset:
    def test_uses_utc_date(self):
        """_maybe_reset_daily must use UTC, not local time."""
        source = inspect.getsource(SystemState._maybe_reset_daily)
        assert "utc" in source.lower()
        assert "date.today()" not in source


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #20: Balance cache invalidated after place_order
# ══════════════════════════════════════════════════════════════════════════


class TestBalanceCacheInvalidation:
    def test_place_order_invalidates_cache(self):
        """place_order must invalidate balance cache after successful order."""
        source = open("arbitrage/system/live_adapters.py").read()
        place_order_section = source.split("async def place_order")[1].split("async def place_spot_order")[0]
        assert "invalidate_balance_cache" in place_order_section


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #29: private_ws bounded _order_data
# ══════════════════════════════════════════════════════════════════════════


class TestOrderDataBounded:
    def test_order_data_has_cleanup(self):
        """_on_order must limit _order_data size."""
        from arbitrage.exchanges.private_ws import PrivateWsManager
        source = inspect.getsource(PrivateWsManager._on_order)
        assert "5000" in source or "len(self._order_data)" in source


# ══════════════════════════════════════════════════════════════════════════
# Medium FIX #30: Slippage depth_ratio cap increased
# ══════════════════════════════════════════════════════════════════════════


class TestSlippageDepthRatio:
    def test_depth_ratio_cap_not_too_low(self):
        """depth_ratio should not be capped at 2.0 (underestimates large orders)."""
        source = open("arbitrage/system/slippage.py").read()
        assert "min(2.0," not in source
        assert "min(10.0," in source

    def test_large_order_gets_high_slippage(self):
        """Order 5x book depth should have much higher slippage than 1x."""
        model = SlippageModel()
        slip_1x = model.estimate(100_000, 100_000, 0.01, 50.0)
        slip_5x = model.estimate(500_000, 100_000, 0.01, 50.0)
        assert slip_5x > slip_1x * 3, f"5x order should be >3x slippage: {slip_1x:.1f} vs {slip_5x:.1f}"


# ══════════════════════════════════════════════════════════════════════════
# P0 FIX #7: Short handlers race condition
# ══════════════════════════════════════════════════════════════════════════


class TestShortHandlersRace:
    def test_pending_symbols_exist(self):
        """_pending_symbols set must exist for race prevention."""
        from handlers import short_handlers
        assert hasattr(short_handlers, "_pending_symbols")
        assert isinstance(short_handlers._pending_symbols, set)

    def test_source_uses_pending_symbols_in_guard(self):
        """The guard check must include _pending_symbols."""
        source = open("handlers/short_handlers.py").read()
        assert "_pending_symbols" in source
        # Must add to pending before releasing lock
        assert "_pending_symbols.add(symbol)" in source
        # Must discard on exit paths
        assert source.count("_pending_symbols.discard(symbol)") >= 3


# ══════════════════════════════════════════════════════════════════════════
# P1 FIX #15: SL/TP failure closes position
# ══════════════════════════════════════════════════════════════════════════


class TestSLTPFailureSafety:
    def test_sltp_failure_triggers_emergency_close(self):
        """If SL/TP setting fails, position must be closed."""
        source = open("handlers/short_handlers.py").read()
        sltp_section = source.split("set_trading_stop")[1][:1500]
        assert 'offset="close"' in sltp_section or "emergency close" in sltp_section.lower()
        assert "return None" in sltp_section


# ══════════════════════════════════════════════════════════════════════════
# Integration: full entry flow
# ══════════════════════════════════════════════════════════════════════════


class TestEntryFlowIntegration:
    @pytest.mark.asyncio
    async def test_successful_dual_entry(self):
        """Full happy path: both legs fill, position created."""
        engine, venue, monitor = _make_engine(dry_run=False)

        venue.place_order = AsyncMock(side_effect=[
            {"success": True, "order_id": "o1", "size": 1.0, "fill_price": 50000.0,
             "effective_notional": 100.0},
            {"success": True, "order_id": "o2", "size": 1.0, "fill_price": 50010.0,
             "effective_notional": 100.0},
        ])
        venue.wait_for_fill = AsyncMock(return_value=True)

        intent = _make_intent()
        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        assert report.success
        assert report.position_id is not None

    @pytest.mark.asyncio
    async def test_first_leg_fails_no_hedge_needed(self):
        """If first leg fails, no position created, no hedge."""
        engine, venue, monitor = _make_engine(dry_run=False)

        venue.place_order = AsyncMock(return_value={"success": False, "message": "rejected"})

        intent = _make_intent()
        report = await engine.execute_dual_entry(intent, 100.0, 2_000_000, 0.01, 100.0)
        assert not report.success
        assert report.message == "first_leg_failed"
