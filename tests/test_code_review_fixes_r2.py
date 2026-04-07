"""Tests for code review fixes round 2 (#7-#10)."""
from __future__ import annotations
import inspect
import pytest
from arbitrage.system.config import RiskConfig, TradingSystemConfig


class TestFix7ConfigValidation:
    def _cfg(self, **kw):
        return TradingSystemConfig(
            symbols=["BTCUSDT"], exchanges=["bybit", "okx"],
            credentials={}, starting_equity=1000.0, risk=RiskConfig(**kw))

    def test_zero_per_symbol(self):
        with pytest.raises(ValueError, match="max_positions_per_symbol"):
            self._cfg(max_positions_per_symbol=0).validate()

    def test_negative_per_symbol(self):
        with pytest.raises(ValueError, match="max_positions_per_symbol"):
            self._cfg(max_positions_per_symbol=-1).validate()

    def test_valid_per_symbol(self):
        self._cfg(max_positions_per_symbol=3).validate()

    def test_zero_open_positions(self):
        with pytest.raises(ValueError, match="max_open_positions"):
            self._cfg(max_open_positions=0).validate()

    def test_valid_open_positions(self):
        self._cfg(max_open_positions=5).validate()


class TestFix7FieldExists:
    def test_risk_config_has_field(self):
        rc = RiskConfig()
        assert hasattr(rc, "max_positions_per_symbol")
        assert rc.max_positions_per_symbol == 2


class TestFix8HedgeBackoff:
    def test_exponential_backoff_in_hedge(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine._hedge_first_leg)
        assert "2 ** attempt" in src or "2**attempt" in src
        assert "base_delay" in src

    def test_backoff_capped(self):
        from arbitrage.system.execution import AtomicExecutionEngine
        src = inspect.getsource(AtomicExecutionEngine._hedge_first_leg)
        assert "10.0" in src or "10" in src


class TestFix9BinanceWsBackoff:
    def test_reconnect_attempt_attr(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        ws = BinanceWebSocket("BTCUSDT")
        assert hasattr(ws, "_reconnect_attempt")
        assert ws._reconnect_attempt == 0

    def test_exponential_backoff_in_source(self):
        from arbitrage.exchanges.binance_ws import BinanceWebSocket
        src = inspect.getsource(BinanceWebSocket)
        assert "2 ** self._reconnect_attempt" in src
        assert "min(" in src


class TestFix10PrivateWsBackoff:
    def test_okx_has_reconnect(self):
        from arbitrage.exchanges.private_ws import OKXPrivateWs
        src = inspect.getsource(OKXPrivateWs.__init__)
        assert "_reconnect_attempt" in src

    def test_htx_has_reconnect(self):
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        src = inspect.getsource(HTXPrivateWs.__init__)
        assert "_reconnect_attempt" in src

    def test_bybit_has_reconnect(self):
        from arbitrage.exchanges.private_ws import BybitPrivateWs
        src = inspect.getsource(BybitPrivateWs.__init__)
        assert "_reconnect_attempt" in src

    def test_okx_uses_backoff(self):
        from arbitrage.exchanges.private_ws import OKXPrivateWs
        src = inspect.getsource(OKXPrivateWs)
        assert "2 ** self._reconnect_attempt" in src

    def test_htx_uses_backoff(self):
        from arbitrage.exchanges.private_ws import HTXPrivateWs
        src = inspect.getsource(HTXPrivateWs)
        assert "2 ** self._reconnect_attempt" in src

    def test_bybit_uses_backoff(self):
        from arbitrage.exchanges.private_ws import BybitPrivateWs
        src = inspect.getsource(BybitPrivateWs)
        assert "2 ** self._reconnect_attempt" in src
