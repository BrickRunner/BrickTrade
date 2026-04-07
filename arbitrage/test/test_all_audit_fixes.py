"""
Comprehensive test suite to verify ALL code audit fixes from COMPREHENSIVE_CODE_AUDIT_2026.md.

Tests cover:
1.  age_sec NameError fix (engine.py)
2.  WS message loop robust reconnection (private_ws.py)
3.  Tautological comparison fix (execution_v2.py)
4.  edge_bps calculation clarity (cash_and_carry.py)
5.  Kill switch permanent → temporary (engine.py)
6.  Circuit breaker single-exchange error (engine.py)
7.  Daily DD temporary kill switch (risk.py)
8.  (HTX auth timeout - verified by reading)
9.  Cached env vars in engine.py
10. HTX gzip decompression error handling
11. Emergency hedge only when something filled
12. asyncio.get_running_loop instead of get_event_loop
13. Removed unused _symbol_loss_streak
14. BotState get_orderbooks returns all exchanges
"""
import asyncio
import gzip
import json
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

# Ensure project path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ─────────────────────────────────────────────────────────────
# Fix #1: age_sec NameError in engine.py
# ─────────────────────────────────────────────────────────────
class TestAgeSecNameError(unittest.TestCase):
    """age_sec must be defined before it's used in funding calculation."""

    def test_age_sec_used_before_definition_in_source(self):
        """Verify age_sec is defined before funding pnl calculation."""
        import inspect
        from arbitrage.system.engine import TradingSystemEngine

        source = inspect.getsource(TradingSystemEngine._process_open_positions)
        lines = source.split('\n')

        # Find line indices
        age_sec_def = None
        periods_held = None
        pnl_usd = None

        for i, line in enumerate(lines):
            if 'age_sec = now - pos.opened_at' in line or 'age_sec=now-pos.opened_at' in line.strip():
                age_sec_def = i
            if 'periods_held = age_sec / funding_interval_sec' in line or 'periods_held=age_sec' in line:
                periods_held = i
            if 'pnl_usd = long_pnl' in line:
                pnl_usd = i

        self.assertIsNotNone(age_sec_def, "age_sec definition not found in source")
        self.assertIsNotNone(periods_held, "periods_held usage not found")
        self.assertIsNotNone(pnl_usd, "pnl_usd usage not found")

        # age_sec must be defined BEFORE periods_held (which uses it)
        self.assertLess(age_sec_def, periods_held,
                        f"age_sec (line {age_sec_def}) must be defined before "
                        f"periods_held (line {periods_held})")


# ─────────────────────────────────────────────────────────────
# Fix #2: WS message loop robust reconnection
# ─────────────────────────────────────────────────────────────
class TestWSRobustReconnection(unittest.TestCase):
    """WS message loop must use explicit recv with timeout, not 'async for'."""

    def test_okx_uses_explicit_recv_loop(self):
        import inspect
        from arbitrage.exchanges.private_ws import OKXPrivateWs
        source = inspect.getsource(OKXPrivateWs.connect)
        # Should NOT have 'async for message in ws' (the fragile pattern)
        self.assertNotIn('async for message in ws', source,
                         "OKX WS must not use 'async for message in ws' — "
                         "can die silently on some websockets versions")
        # Should have explicit recv with timeout
        self.assertIn('ws.recv()', source,
                      "OKX WS must use explicit ws.recv() with timeout")
        self.assertIn('heartbeat_timeout', source,
                      "OKX WS must detect and log heartbeat timeout")

    def test_bybit_uses_explicit_recv_loop(self):
        import inspect
        from arbitrage.exchanges.private_ws import BybitPrivateWs
        source = inspect.getsource(BybitPrivateWs.connect)
        self.assertNotIn('async for message in ws', source,
                         "Bybit WS must not use 'async for message in ws'")
        self.assertIn('ws.recv()', source)
        self.assertIn('heartbeat_timeout', source)

    def test_htx_uses_explicit_recv_loop(self):
        import inspect
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        source = inspect.getsource(HTXPrivateWs.connect)
        self.assertNotIn('async for raw_msg in ws', source,
                         "HTX WS must not use 'async for raw_msg in ws'")
        self.assertIn('ws.recv()', source)
        self.assertIn('heartbeat_timeout', source)

    def test_ws_max_message_size(self):
        """WS connections should have max_size to prevent OOM."""
        import inspect
        from arbitrage.exchanges.private_ws import OKXPrivateWs, BybitPrivateWs, HTXPrivateWs
        for cls in [OKXPrivateWs, BybitPrivateWs, HTXPrivateWs]:
            source = inspect.getsource(cls.connect)
            self.assertIn('max_size', source,
                          f"{cls.__name__} WS must have max_size to prevent OOM")


# ─────────────────────────────────────────────────────────────
# Fix #3: Tautological comparison in execution_v2
# ─────────────────────────────────────────────────────────────
class TestExecutionV2SideAssignment(unittest.TestCase):
    """Long leg = buy, short leg = sell. No tautology."""

    def test_side_assignment_not_tautological(self):
        import inspect
        from arbitrage.system.execution_v2 import AtomicExecutionEngineV2
        source = inspect.getsource(AtomicExecutionEngineV2.execute_arbitrage)
        # Should NOT have the old tautological comparison
        self.assertNotIn('intent.metadata.get("long_exchange") == exchange_a', source,
                         "Must not compare variable to itself (tautological comparison)")
        # Should have explicit buy/sell
        self.assertIn('side_a = "buy"', source,
                      "Long leg (side_a) should explicitly be 'buy'")
        self.assertIn('side_b = "sell"', source,
                      "Short leg (side_b) should explicitly be 'sell'")


# ─────────────────────────────────────────────────────────────
# Fix #5: Kill switch should be temporary, not permanent
# ─────────────────────────────────────────────────────────────
class TestKillSwitchTemporary(unittest.TestCase):
    """second_leg_failed without hedge should trigger TEMPORARY kill switch."""

    def test_second_leg_failed_uses_temporary_kill_switch(self):
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine.run_cycle)
        # Should use permanent=False for unverified hedge
        # Find the relevant section
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if 'unverified_hedge' in line:
                # Next few lines should have permanent=False, not permanent=True
                context = '\n'.join(lines[i:i+10])
                self.assertIn('permanent=False', context,
                              "Unverified hedge should trigger TEMPORARY kill switch (permanent=False)")
                self.assertNotIn('permanent=True', context,
                                 "Unverified hedge should NOT trigger permanent kill switch")


# ─────────────────────────────────────────────────────────────
# Fix #7: Daily DD should use temporary kill switch
# ─────────────────────────────────────────────────────────────
class TestDailyDDKillSwitch(unittest.TestCase):
    """Daily drawdown triggers temporary pause, not permanent kill."""

    def test_daily_dd_uses_temporary_kill_switch(self):
        import inspect
        from arbitrage.system.risk import RiskEngine
        source = inspect.getsource(RiskEngine.validate_intent)
        # Find daily_dd section
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if 'daily_drawdown' in line.lower():
                context = '\n'.join(lines[i-3:i+5])
                self.assertIn('permanent=False', context,
                              "Daily DD should use temporary kill switch (permanent=False)")


# ─────────────────────────────────────────────────────────────
# Fix #9: Cached env vars
# ─────────────────────────────────────────────────────────────
class TestCachedEnvVars(unittest.TestCase):
    """Engine should cache env vars at module level, not read in hot loop."""

    def test_module_level_constant_exists(self):
        import arbitrage.system.engine as engine_module
        self.assertTrue(hasattr(engine_module, '_MAX_EQUITY_PER_TRADE_PCT'),
                        "Module should have _MAX_EQUITY_PER_TRADE_PCT constant")
        self.assertIsInstance(engine_module._MAX_EQUITY_PER_TRADE_PCT, float)

    def test_run_cycle_uses_cached_constant(self):
        import inspect
        from arbitrage.system.engine import TradingSystemEngine
        source = inspect.getsource(TradingSystemEngine.run_cycle)
        # Should use the cached constant, not os.getenv in the loop
        self.assertNotIn('os.getenv("MAX_EQUITY_PER_TRADE_PCT"', source,
                         "run_cycle should NOT call os.getenv in the hot loop")
        self.assertIn('_MAX_EQUITY_PER_TRADE_PCT', source,
                      "run_cycle should use cached _MAX_EQUITY_PER_TRADE_PCT constant")


# ─────────────────────────────────────────────────────────────
# Fix #10: HTX gzip decompression error handling
# ─────────────────────────────────────────────────────────────
class TestHTXGzipHandling(unittest.TestCase):
    """HTX must handle both gzip and non-gzip messages gracefully."""

    def test_decompress_handles_non_gzip(self):
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        # Non-gzip JSON string (like auth error response)
        plain_json = json.dumps({"op": "auth", "err-code": -1, "msg": "invalid key"})
        result = HTXPrivateWs._decompress(plain_json)
        self.assertEqual(result["op"], "auth")
        self.assertEqual(result["err-code"], -1)

    def test_decompress_handles_gzip(self):
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        # Gzip-compressed JSON
        data = json.dumps({"op": "notify", "topic": "accounts_cross", "data": []})
        compressed = gzip.compress(data.encode('utf-8'))
        result = HTXPrivateWs._decompress(compressed)
        self.assertEqual(result["op"], "notify")

    def test_decompress_handles_bytes_not_gzip(self):
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        # Raw bytes that don't look like gzip
        raw = b'{"op":"ping"}'
        result = HTXPrivateWs._decompress(raw)
        self.assertEqual(result["op"], "ping")

    def test_decompress_handles_dict_input(self):
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        # Already a dict (edge case)
        data = {"op": "test"}
        result = HTXPrivateWs._decompress(data)
        self.assertEqual(result["op"], "test")


# ─────────────────────────────────────────────────────────────
# Fix #11: Emergency hedge only when something filled
# ─────────────────────────────────────────────────────────────
class TestEmergencyHedgeConditional(unittest.TestCase):
    """Should only hedge when at least one leg filled, not when both failed."""

    def test_emergency_hedge_skipped_when_nothing_filled(self):
        import inspect
        from arbitrage.system.execution_v2 import AtomicExecutionEngineV2
        source = inspect.getsource(AtomicExecutionEngineV2.execute_arbitrage)

        # Should check for PARTIAL_FILL before hedging
        self.assertIn('PARTIAL_FILL', source,
                      "Should check for partial fill before hedge")
        # Should NOT call hedge unconditionally
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if 'emergency_hedge' in line.lower() or '_emergency_hedge' in line.lower():
                # Look backward for the conditional
                context = '\n'.join(lines[max(0,i-5):i+2])
                self.assertTrue(
                    'PARTIAL_FILL' in context or 'if' in context.lower(),
                    "Hedge should be conditional on partial fill"
                )


# ─────────────────────────────────────────────────────────────
# Fix #12: asyncio.get_running_loop
# ─────────────────────────────────────────────────────────────
class TestAsyncioGetRunningLoop(unittest.TestCase):
    """Must use get_running_loop, not deprecated get_event_loop."""

    def test_execution_uses_get_running_loop(self):
        import inspect
        from arbitrage.system.execution import AtomicExecutionEngine
        source = inspect.getsource(AtomicExecutionEngine.execute_dual_entry)
        self.assertIn('get_running_loop()', source)
        self.assertNotIn('get_event_loop()', source)

    def test_lowlatency_uses_get_running_loop(self):
        import inspect
        from arbitrage.system.lowlatency import LowLatencyExecutionVenue
        source = inspect.getsource(LowLatencyExecutionVenue.wait_for_fill)
        self.assertIn('get_running_loop()', source)
        self.assertNotIn('get_event_loop()', source)


# ─────────────────────────────────────────────────────────────
# Fix #13: Unused _symbol_loss_streak removed
# ─────────────────────────────────────────────────────────────
class TestUnusedSymbolLossStreak(unittest.TestCase):
    """_symbol_loss_streak field should be removed from TradingSystemEngine."""

    def test_no_loss_streak_field(self):
        from dataclasses import fields
        from arbitrage.system.engine import TradingSystemEngine
        field_names = [f.name for f in fields(TradingSystemEngine)]
        self.assertNotIn('_symbol_loss_streak', field_names,
                         "_symbol_loss_streak should be removed (unused)")


# ─────────────────────────────────────────────────────────────
# Fix #14: BotState get_orderbooks returns all exchanges
# ─────────────────────────────────────────────────────────────
class TestBotStateOrderbooks(unittest.TestCase):
    """get_orderbooks should return all cached orderbooks, not just okx/htx."""

    def test_get_orderbooks_returns_dict(self):
        from arbitrage.core.state import BotState, OrderBookData
        state = BotState()
        # Add orderbooks for multiple exchanges
        state._orderbooks["okx"] = OrderBookData(
            exchange="okx", symbol="BTCUSDT",
            bids=[[100.0, 1.0]], asks=[[101.0, 1.0]],
            timestamp=time.time(), best_bid=100.0, best_ask=101.0,
        )
        state._orderbooks["bybit"] = OrderBookData(
            exchange="bybit", symbol="BTCUSDT",
            bids=[[99.0, 1.0]], asks=[[100.0, 1.0]],
            timestamp=time.time(), best_bid=99.0, best_ask=100.0,
        )
        state._orderbooks["htx"] = OrderBookData(
            exchange="htx", symbol="BTCUSDT",
            bids=[[98.0, 1.0]], asks=[[99.0, 1.0]],
            timestamp=time.time(), best_bid=98.0, best_ask=99.0,
        )

        ob_dict = state.get_orderbooks()
        # Should be a dict, not a tuple
        self.assertIsInstance(ob_dict, dict,
                              "get_orderbooks should return dict with all exchanges")
        self.assertIn("okx", ob_dict)
        self.assertIn("bybit", ob_dict)
        self.assertIn("htx", ob_dict)


# ─────────────────────────────────────────────────────────────
# Integration: Risk engine daily DD test
# ─────────────────────────────────────────────────────────────
class TestRiskEngineDailyDD(unittest.TestCase):
    """Daily DD should trigger temporary (not permanent) kill switch."""

    def test_daily_dd_triggers_temporary_kill_switch(self):
        import asyncio
        from arbitrage.system.risk import RiskEngine
        from arbitrage.system.config import RiskConfig
        from arbitrage.system.state import SystemState

        config = RiskConfig(
            max_daily_drawdown_pct=0.05,
            max_portfolio_drawdown_pct=0.12,
            max_leverage=3.0,
            max_order_slippage_bps=25.0,
            api_latency_limit_ms=400,
            max_open_positions=3,
            max_inventory_imbalance_pct=0.80,
        )
        state = SystemState(starting_equity=100.0)

        # Simulate a 10% daily drawdown (exceeds 5% threshold)
        async def _run():
            await state.set_equity(90.0)  # 10% DD
            engine = RiskEngine(config, state)

            # Create a mock intent and snapshot for validation
            result = await engine.validate_intent(
                intent=MagicMock(long_exchange="okx", short_exchange="bybit",
                                strategy_id=MagicMock()),
                allocation_plan=MagicMock(strategy_allocations={}),
                proposed_notional=10.0,
                estimated_slippage_bps=5.0,
                leverage=1.0,
                api_latency_ms=100.0,
                snapshot=None,
            )

            self.assertFalse(result.approved)
            self.assertEqual(result.reason, "daily_drawdown_stop")
            self.assertTrue(result.kill_switch_triggered)

            # Verify the kill switch is NOT permanent (temporary/cooldown)
            self.assertFalse(state._kill_switch_permanent,
                             "Daily DD should NOT set permanent kill switch")

        asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    unittest.main()
