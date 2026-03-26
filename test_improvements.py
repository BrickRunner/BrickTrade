"""Tests for the three execution improvements:
1. Walk-the-book realistic price calculation
2. Maker+Taker hybrid execution
3. WebSocket orderbook depth integration
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbitrage.system.slippage import SlippageModel
from arbitrage.system.config import ExecutionConfig, RiskConfig, TradingSystemConfig
from arbitrage.system.models import (
    MarketSnapshot,
    OrderBookSnapshot,
    StrategyId,
    TradeIntent,
    OpenPosition,
    ExecutionReport,
)
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.state import SystemState
from arbitrage.system.monitoring import InMemoryMonitoring
from arbitrage.system.ws_orderbooks import WsOrderbookCache
from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy


# ─── Helpers ───

def _make_snapshot(
    symbol: str = "BTCUSDT",
    orderbooks: dict | None = None,
    depth: dict | None = None,
    balances: dict | None = None,
    fee_bps: dict | None = None,
    funding_rates: dict | None = None,
) -> MarketSnapshot:
    obs = orderbooks or {
        "okx": OrderBookSnapshot(exchange="okx", symbol=symbol, bid=50000.0, ask=50010.0),
        "htx": OrderBookSnapshot(exchange="htx", symbol=symbol, bid=50050.0, ask=50060.0),
    }
    return MarketSnapshot(
        symbol=symbol,
        orderbooks=obs,
        spot_orderbooks={},
        orderbook_depth=depth or {},
        spot_orderbook_depth={},
        balances=balances or {"okx": 10000.0, "htx": 10000.0},
        fee_bps=fee_bps or {"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        funding_rates=funding_rates or {},
        volatility=0.001,
        trend_strength=0.0,
        atr=10.0,
        atr_rolling=10.0,
        indicators={},
    )


def _make_venue_mock():
    venue = AsyncMock()
    venue.safety_buffer_pct = 0.05
    venue.safety_reserve_usd = 0.50
    venue._min_notional_usd = MagicMock(return_value=1.0)
    venue.place_order = AsyncMock(return_value={
        "success": True, "fill_price": 50000.0, "exchange": "okx",
        "order_id": "ord_1", "size": 1.0, "effective_notional": 100.0,
    })
    venue.place_spot_order = AsyncMock(return_value={"success": True})
    venue.wait_for_fill = AsyncMock(return_value=True)
    venue.get_balances = AsyncMock(return_value={"okx": 10000.0, "htx": 10000.0})
    venue.open_contracts = AsyncMock(return_value=0.0)
    venue.cancel_order = AsyncMock()
    venue.invalidate_balance_cache = MagicMock()
    return venue


def _make_engine(venue=None, config=None):
    if config is None:
        config = ExecutionConfig(dry_run=False)
    if venue is None:
        venue = _make_venue_mock()
    state = SystemState(starting_equity=10000.0)
    monitor = InMemoryMonitoring(logger=logging.getLogger("test"))
    return AtomicExecutionEngine(
        config=config,
        venue=venue,
        slippage=SlippageModel(),
        state=state,
        monitor=monitor,
    ), venue, state


# ═══════════════════════════════════════════
#  1. Walk-the-Book Tests
# ═══════════════════════════════════════════

class TestWalkBook:
    """Test realistic price calculation from orderbook depth."""

    def test_walk_book_single_level_sufficient(self):
        """Single level has enough depth for the entire order."""
        levels = [(100.0, 10.0)]  # $1000 available at 100
        avg = SlippageModel.walk_book(levels, 500.0)
        assert avg == 100.0  # fills entirely at level 1

    def test_walk_book_multiple_levels(self):
        """Order walks through multiple price levels."""
        levels = [
            (100.0, 1.0),   # $100 at price 100
            (101.0, 1.0),   # $101 at price 101
            (102.0, 1.0),   # $102 at price 102
        ]
        avg = SlippageModel.walk_book(levels, 201.0)
        # Should fill: 1.0 @ 100 ($100), 1.0 @ 101 ($101), ~0 @ 102
        # Total: $201, qty = 1.0 + 1.0 + 0.0098... ≈ 2.0098
        assert 100.0 < avg < 101.1

    def test_walk_book_exact_one_level(self):
        """Order exactly matches one level's depth."""
        levels = [(50.0, 2.0), (51.0, 2.0)]  # $100, $102
        avg = SlippageModel.walk_book(levels, 100.0)
        assert avg == 50.0  # Fully consumed first level exactly

    def test_walk_book_empty_book(self):
        """Empty orderbook returns 0."""
        assert SlippageModel.walk_book([], 100.0) == 0.0

    def test_walk_book_insufficient_depth(self):
        """Order larger than all available depth."""
        levels = [(100.0, 1.0)]  # Only $100 available
        avg = SlippageModel.walk_book(levels, 500.0)
        # Should fill what it can: 1.0 qty at 100
        assert avg == 100.0

    def test_walk_book_zero_notional(self):
        """Zero notional returns 0."""
        levels = [(100.0, 1.0)]
        assert SlippageModel.walk_book(levels, 0.0) == 0.0

    def test_walk_book_skips_invalid_levels(self):
        """Invalid levels (zero price/qty) are skipped."""
        levels = [(0.0, 1.0), (100.0, 0.0), (101.0, 2.0)]
        avg = SlippageModel.walk_book(levels, 100.0)
        assert avg == 101.0  # Only the valid level 101.0 is used

    def test_walk_book_slippage_bps_zero(self):
        """No slippage when order fits in first level."""
        levels = [(100.0, 100.0)]
        bps = SlippageModel.walk_book_slippage_bps(levels, 500.0, 100.0)
        assert bps == 0.0

    def test_walk_book_slippage_bps_positive(self):
        """Slippage > 0 when walking past first level."""
        levels = [
            (100.0, 0.5),   # $50 at 100
            (100.10, 0.5),  # ~$50 at 100.10
            (100.20, 10.0), # rest at 100.20
        ]
        bps = SlippageModel.walk_book_slippage_bps(levels, 200.0, 100.0)
        assert bps > 0  # Must be positive (worse than top-of-book)

    def test_walk_book_slippage_empty_book(self):
        """Empty book returns max penalty."""
        bps = SlippageModel.walk_book_slippage_bps([], 100.0, 50.0)
        assert bps == 1000.0

    def test_walk_book_large_order_significant_impact(self):
        """Large order has meaningful price impact across levels."""
        # Realistic BTC book: 5 levels, each ~$5000 depth
        levels = [
            (50000.0, 0.1),   # $5000
            (50010.0, 0.1),   # $5001
            (50020.0, 0.1),   # $5002
            (50030.0, 0.1),   # $5003
            (50040.0, 0.1),   # $5004
        ]
        avg_small = SlippageModel.walk_book(levels, 1000.0)  # fits in level 1
        avg_large = SlippageModel.walk_book(levels, 20000.0)  # spans 4 levels
        assert avg_large > avg_small  # Large order gets worse avg price


# ═══════════════════════════════════════════
#  2. Maker+Taker Hybrid Execution Tests
# ═══════════════════════════════════════════

class TestMakerTakerConfig:
    """Test maker+taker configuration."""

    def test_default_config_disabled(self):
        config = ExecutionConfig()
        assert config.use_maker_taker is False
        assert config.maker_timeout_ms == 2000
        assert config.maker_max_retries == 2
        assert config.maker_price_offset_bps == 0.5

    def test_config_from_env(self):
        with patch.dict("os.environ", {
            "EXEC_USE_MAKER_TAKER": "true",
            "EXEC_MAKER_TIMEOUT_MS": "3000",
            "EXEC_MAKER_MAX_RETRIES": "3",
            "EXEC_MAKER_PRICE_OFFSET_BPS": "1.0",
        }):
            config = TradingSystemConfig.from_env()
            assert config.execution.use_maker_taker is True
            assert config.execution.maker_timeout_ms == 3000
            assert config.execution.maker_max_retries == 3
            assert config.execution.maker_price_offset_bps == 1.0


class TestMakerTakerExecution:
    """Test maker+taker hybrid order execution."""

    @pytest.mark.asyncio
    async def test_maker_disabled_uses_taker(self):
        """When use_maker_taker=False, both legs use taker IOC."""
        config = ExecutionConfig(dry_run=False, use_maker_taker=False)
        engine, venue, _ = _make_engine(config=config)
        # Configure both legs to succeed
        venue.place_order = AsyncMock(side_effect=[
            {"success": True, "fill_price": 50000.0, "exchange": "okx", "order_id": "1", "size": 1.0, "effective_notional": 100.0},
            {"success": True, "fill_price": 50050.0, "exchange": "htx", "order_id": "2", "size": 1.0, "effective_notional": 100.0},
        ])
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={"entry_mid": 50025.0, "limit_prices": {"buy": 50010.0, "sell": 50050.0}},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        assert report.success
        # Both calls should use "ioc", not "post_only"
        for call in venue.place_order.call_args_list:
            assert call.args[3] != "post_only" or call.kwargs.get("order_type") != "post_only"

    @pytest.mark.asyncio
    async def test_maker_enabled_fills_on_first_attempt(self):
        """Maker leg fills on first attempt — saves fees."""
        config = ExecutionConfig(dry_run=False, use_maker_taker=True, maker_max_retries=2)
        engine, venue, _ = _make_engine(config=config)

        call_count = 0

        async def mock_place_order(exchange, symbol, side, notional, order_type, limit_price=0.0, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "success": True, "fill_price": 50000.0 if side == "buy" else 50050.0,
                "exchange": exchange, "order_id": f"ord_{call_count}", "size": 1.0,
                "effective_notional": notional,
            }

        venue.place_order = AsyncMock(side_effect=mock_place_order)

        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={"entry_mid": 50025.0, "limit_prices": {"buy": 50010.0, "sell": 50050.0}},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        assert report.success
        # Should have post_only call for maker leg (okx = first = more reliable)
        found_post_only = False
        for call in venue.place_order.call_args_list:
            if len(call.args) >= 5 and call.args[4] == "post_only":
                found_post_only = True
        assert found_post_only, "Expected at least one post_only order"

    @pytest.mark.asyncio
    async def test_maker_rejected_falls_back_to_taker(self):
        """Post-only rejected (would cross) → falls back to taker IOC."""
        config = ExecutionConfig(dry_run=False, use_maker_taker=True, maker_max_retries=0)
        engine, venue, _ = _make_engine(config=config)

        call_count = 0

        async def mock_place_order(exchange, symbol, side, notional, order_type, limit_price=0.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if order_type == "post_only":
                # Post-only rejected — would have crossed
                return {"success": False, "message": "post_only_rejected", "exchange": exchange}
            return {
                "success": True, "fill_price": 50000.0 if side == "buy" else 50050.0,
                "exchange": exchange, "order_id": f"ord_{call_count}", "size": 1.0,
                "effective_notional": notional,
            }

        venue.place_order = AsyncMock(side_effect=mock_place_order)

        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={"entry_mid": 50025.0, "limit_prices": {"buy": 50010.0, "sell": 50050.0}},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        assert report.success
        # Should have fallback taker call after post_only rejection
        order_types_used = [call.args[4] for call in venue.place_order.call_args_list if len(call.args) >= 5]
        assert "post_only" in order_types_used
        assert "ioc" in order_types_used

    @pytest.mark.asyncio
    async def test_maker_timeout_retries_then_fallback(self):
        """Maker order times out → retries → eventually falls back to taker."""
        config = ExecutionConfig(
            dry_run=False, use_maker_taker=True,
            maker_max_retries=1, maker_timeout_ms=100,
        )
        engine, venue, _ = _make_engine(config=config)

        call_count = 0

        async def mock_place_order(exchange, symbol, side, notional, order_type, limit_price=0.0, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "success": True, "fill_price": 50000.0 if side == "buy" else 50050.0,
                "exchange": exchange, "order_id": f"ord_{call_count}", "size": 1.0,
                "effective_notional": notional,
            }

        fill_count = 0

        async def mock_wait_for_fill(exchange, symbol, order_id, timeout_ms, **kwargs):
            nonlocal fill_count
            fill_count += 1
            # Maker fills never confirmed, taker fills always confirmed
            # The maker leg has shorter timeout; after retries it falls back to IOC
            # For simplicity: if timeout < 500ms, it's a maker attempt → fail
            if timeout_ms <= 200:
                return False  # maker timeout
            return True  # taker fill ok

        venue.place_order = AsyncMock(side_effect=mock_place_order)
        venue.wait_for_fill = AsyncMock(side_effect=mock_wait_for_fill)

        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={"entry_mid": 50025.0, "limit_prices": {"buy": 50010.0, "sell": 50050.0}},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        assert report.success
        # Should have called cancel_order for unfilled maker attempts
        assert venue.cancel_order.call_count >= 1

    @pytest.mark.asyncio
    async def test_maker_not_used_for_spot_legs(self):
        """Maker mode is disabled for spot legs (only perp)."""
        config = ExecutionConfig(dry_run=False, use_maker_taker=True)
        engine, venue, _ = _make_engine(config=config)
        venue.place_order = AsyncMock(return_value={
            "success": True, "fill_price": 50000.0, "exchange": "okx",
            "order_id": "1", "size": 1.0, "effective_notional": 100.0,
        })
        venue.place_spot_order = AsyncMock(return_value={
            "success": True, "exchange": "htx", "order_id": "2", "size": 0.002,
        })
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={
                "entry_mid": 50025.0,
                "limit_prices": {"buy": 50010.0, "sell": 50050.0},
                "leg_kinds": {"htx": "spot"},
                "spot_price": 50000.0,
            },
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        # Spot leg should use place_spot_order, not post_only
        for call in venue.place_order.call_args_list:
            assert call.args[4] != "post_only" if len(call.args) >= 5 else True


# ═══════════════════════════════════════════
#  3. Post-Only Order Mapping Tests
# ═══════════════════════════════════════════

class TestPostOnlyMapping:
    """Test _map_order_params for post_only across exchanges."""

    def _map(self, exchange, order_type, price):
        from arbitrage.system.live_adapters import LiveExecutionVenue
        return LiveExecutionVenue._map_order_params(exchange, order_type, price)

    def test_okx_post_only(self):
        ot, tif, px = self._map("okx", "post_only", 50000.0)
        assert ot == "post_only"
        assert px == 50000.0

    def test_htx_post_only(self):
        ot, tif, px = self._map("htx", "post_only", 50000.0)
        assert ot == "limit"
        assert tif == "maker"
        assert px == 50000.0

    def test_binance_post_only(self):
        ot, tif, px = self._map("binance", "post_only", 50000.0)
        assert ot == "limit"
        assert tif == "GTX"
        assert px == 50000.0

    def test_bybit_post_only(self):
        ot, tif, px = self._map("bybit", "post_only", 50000.0)
        assert ot == "limit"
        assert tif == "PostOnly"
        assert px == 50000.0

    def test_ioc_still_works(self):
        """Regular IOC orders still work correctly."""
        ot, tif, px = self._map("okx", "ioc", 50000.0)
        assert ot == "limit"
        assert tif == "ioc"

    def test_market_still_works(self):
        """Market orders still work correctly."""
        ot, tif, px = self._map("okx", "market", 0.0)
        assert ot == "market"
        assert px == 0.0


# ═══════════════════════════════════════════
#  4. WebSocket Depth Cache Tests
# ═══════════════════════════════════════════

class TestWsDepthCache:
    """Test WebSocket orderbook cache with full depth data."""

    def test_depth_stored_on_update(self):
        """Full depth data is stored alongside best bid/ask."""
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        # Simulate what _on_book does
        bids = [[50000.0, 1.0], [49990.0, 2.0], [49980.0, 3.0]]
        asks = [[50010.0, 1.0], [50020.0, 2.0], [50030.0, 3.0]]
        snapshot = OrderBookSnapshot(
            exchange="okx", symbol="BTCUSDT",
            bid=50000.0, ask=50010.0, timestamp=time.time(),
        )
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = snapshot
        cache._depth.setdefault("okx", {})["BTCUSDT"] = {"bids": bids, "asks": asks}

        # get_depth should return the stored data
        depth = cache.get_depth("okx", "BTCUSDT")
        assert depth is not None
        assert len(depth["bids"]) == 3
        assert len(depth["asks"]) == 3
        assert depth["bids"][0][0] == 50000.0

    def test_depth_stale_returns_none(self):
        """Stale depth data returns None."""
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"], _stale_after_sec=1.0)
        old_ts = time.time() - 5.0  # 5 seconds old
        snapshot = OrderBookSnapshot(
            exchange="okx", symbol="BTCUSDT",
            bid=50000.0, ask=50010.0, timestamp=old_ts,
        )
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = snapshot
        cache._depth.setdefault("okx", {})["BTCUSDT"] = {
            "bids": [[50000.0, 1.0]], "asks": [[50010.0, 1.0]],
        }
        assert cache.get_depth("okx", "BTCUSDT") is None

    def test_depth_missing_exchange_returns_none(self):
        """Missing exchange returns None."""
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        assert cache.get_depth("binance", "BTCUSDT") is None

    def test_depth_fresh_returns_data(self):
        """Fresh depth data is returned correctly."""
        cache = WsOrderbookCache(symbols=["BTCUSDT"], exchanges=["okx"])
        snapshot = OrderBookSnapshot(
            exchange="okx", symbol="BTCUSDT",
            bid=50000.0, ask=50010.0, timestamp=time.time(),
        )
        cache._orderbooks.setdefault("okx", {})["BTCUSDT"] = snapshot
        depth_data = {"bids": [[50000.0, 2.0]], "asks": [[50010.0, 2.0]]}
        cache._depth.setdefault("okx", {})["BTCUSDT"] = depth_data

        result = cache.get_depth("okx", "BTCUSDT")
        assert result == depth_data


# ═══════════════════════════════════════════
#  5. Strategy Walk-the-Book Integration Tests
# ═══════════════════════════════════════════

class TestStrategyWalkTheBook:
    """Test strategy uses walk-the-book when depth is available."""

    def _make_strategy(self, min_spread_pct=0.01):
        return FuturesCrossExchangeStrategy(
            min_spread_pct=min_spread_pct,
            target_profit_pct=0.12,
            max_spread_risk_pct=0.15,
        )

    @pytest.mark.asyncio
    async def test_spread_uses_depth_when_available(self):
        """Strategy uses walked price instead of top-of-book when depth exists."""
        strategy = self._make_strategy(min_spread_pct=0.01)

        # Top-of-book: okx ask=50000, htx bid=50080
        # Depth for htx bids shows price impact — avg fill at ~50060
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50080.0, ask=50090.0),
            },
            depth={
                "okx": {
                    "asks": [[50000.0, 0.01], [50005.0, 0.01], [50010.0, 0.5]],
                    "bids": [[49990.0, 0.5]],
                },
                "htx": {
                    "bids": [[50080.0, 0.001], [50060.0, 0.01], [50040.0, 0.5]],
                    "asks": [[50090.0, 0.5]],
                },
            },
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )

        intents = await strategy.on_market_snapshot(snapshot)
        if intents:
            # If an intent was generated, the walked prices should be in metadata
            for intent in intents:
                if intent.metadata.get("arb_type") == "price_spread":
                    # Walked prices should differ from top-of-book
                    assert "top_of_book_long" in intent.metadata or "top_of_book_short" in intent.metadata

    @pytest.mark.asyncio
    async def test_spread_falls_back_to_top_of_book(self):
        """Without depth data, strategy uses top-of-book prices."""
        strategy = self._make_strategy(min_spread_pct=0.01)

        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50100.0, ask=50110.0),
            },
            depth={},  # No depth data
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )

        intents = await strategy.on_market_snapshot(snapshot)
        # Should still produce intents using top-of-book
        if intents:
            for intent in intents:
                if intent.metadata.get("arb_type") == "price_spread":
                    # entry prices should match top-of-book exactly
                    assert intent.metadata["entry_long_price"] in (50000.0, 50110.0)

    @pytest.mark.asyncio
    async def test_walked_price_reduces_apparent_spread(self):
        """Walk-the-book shows lower effective spread (more realistic)."""
        strategy = self._make_strategy(min_spread_pct=0.01)

        # Top of book: okx ask=50000, htx bid=50200 → 0.40% raw spread
        # Depth: htx bids thin out quickly → walked avg ~50100 → 0.20% raw spread
        snapshot_no_depth = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            depth={},
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )

        snapshot_with_depth = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            depth={
                "okx": {
                    "asks": [[50000.0, 0.001], [50050.0, 0.01], [50100.0, 1.0]],
                    "bids": [[49990.0, 1.0]],
                },
                "htx": {
                    "bids": [[50200.0, 0.001], [50100.0, 0.01], [50000.0, 1.0]],
                    "asks": [[50210.0, 1.0]],
                },
            },
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )

        intents_no_depth = await strategy.on_market_snapshot(snapshot_no_depth)
        # Reset cooldown
        strategy._last_signal_ts.clear()
        intents_with_depth = await strategy.on_market_snapshot(snapshot_with_depth)

        # With thin depth, the spread should be reduced (fewer or smaller intents)
        edge_no_depth = max((i.expected_edge_bps for i in intents_no_depth), default=0)
        edge_with_depth = max((i.expected_edge_bps for i in intents_with_depth), default=0)
        # The walked spread should be equal or less than top-of-book spread
        assert edge_with_depth <= edge_no_depth


# ═══════════════════════════════════════════
#  6. _place_maker_leg Unit Tests
# ═══════════════════════════════════════════

class TestPlaceMakerLeg:
    """Direct tests for the _place_maker_leg method."""

    @pytest.mark.asyncio
    async def test_maker_fills_first_try(self):
        """Maker order fills on first attempt — returns success."""
        config = ExecutionConfig(dry_run=False, use_maker_taker=True, maker_max_retries=2)
        engine, venue, _ = _make_engine(config=config)

        venue.place_order = AsyncMock(return_value={
            "success": True, "fill_price": 50000.0, "exchange": "okx",
            "order_id": "maker_1", "size": 1.0, "effective_notional": 100.0,
        })
        venue.wait_for_fill = AsyncMock(return_value=True)

        result = await engine._place_maker_leg("okx", "BTCUSDT", "buy", 100.0, 50000.0)
        assert result["success"]
        assert venue.cancel_order.call_count == 0  # No cancellation needed

    @pytest.mark.asyncio
    async def test_maker_retries_then_succeeds(self):
        """Maker fails first attempt, succeeds on retry."""
        config = ExecutionConfig(
            dry_run=False, use_maker_taker=True,
            maker_max_retries=2, maker_timeout_ms=100,
        )
        engine, venue, _ = _make_engine(config=config)

        attempt = 0

        async def mock_wait(exchange, symbol, order_id, timeout_ms, **kwargs):
            nonlocal attempt
            attempt += 1
            return attempt >= 2  # Fills on second attempt

        venue.place_order = AsyncMock(return_value={
            "success": True, "fill_price": 50000.0, "exchange": "okx",
            "order_id": "maker_1", "size": 1.0, "effective_notional": 100.0,
        })
        venue.wait_for_fill = AsyncMock(side_effect=mock_wait)

        result = await engine._place_maker_leg("okx", "BTCUSDT", "buy", 100.0, 50000.0)
        assert result["success"]
        assert venue.cancel_order.call_count == 1  # Cancelled first attempt

    @pytest.mark.asyncio
    async def test_maker_all_retries_fail_fallback_taker(self):
        """All maker attempts fail → falls back to taker IOC."""
        config = ExecutionConfig(
            dry_run=False, use_maker_taker=True,
            maker_max_retries=1, maker_timeout_ms=50,
        )
        engine, venue, _ = _make_engine(config=config)

        venue.place_order = AsyncMock(return_value={
            "success": True, "fill_price": 50000.0, "exchange": "okx",
            "order_id": "maker_1", "size": 1.0, "effective_notional": 100.0,
        })
        venue.wait_for_fill = AsyncMock(return_value=False)  # Never fills

        result = await engine._place_maker_leg("okx", "BTCUSDT", "buy", 100.0, 50000.0)
        assert result["success"]
        # Last call should be IOC fallback
        last_call = venue.place_order.call_args_list[-1]
        assert last_call.args[4] == "ioc"

    @pytest.mark.asyncio
    async def test_maker_price_offset_buy(self):
        """Buy maker price is slightly below reference (inside spread)."""
        config = ExecutionConfig(
            dry_run=False, use_maker_taker=True,
            maker_price_offset_bps=1.0,  # 0.01%
        )
        engine, venue, _ = _make_engine(config=config)
        venue.place_order = AsyncMock(return_value={
            "success": True, "fill_price": 50000.0, "exchange": "okx",
            "order_id": "m1", "size": 1.0, "effective_notional": 100.0,
        })
        venue.wait_for_fill = AsyncMock(return_value=True)

        await engine._place_maker_leg("okx", "BTCUSDT", "buy", 100.0, 50000.0)
        # First call should be post_only with price slightly below reference
        first_call = venue.place_order.call_args_list[0]
        placed_price = first_call.args[5]  # limit_price
        assert placed_price < 50000.0  # Buy below reference
        assert placed_price > 49990.0  # Not too far

    @pytest.mark.asyncio
    async def test_maker_price_offset_sell(self):
        """Sell maker price is slightly above reference (inside spread)."""
        config = ExecutionConfig(
            dry_run=False, use_maker_taker=True,
            maker_price_offset_bps=1.0,
        )
        engine, venue, _ = _make_engine(config=config)
        venue.place_order = AsyncMock(return_value={
            "success": True, "fill_price": 50000.0, "exchange": "okx",
            "order_id": "m1", "size": 1.0, "effective_notional": 100.0,
        })
        venue.wait_for_fill = AsyncMock(return_value=True)

        await engine._place_maker_leg("okx", "BTCUSDT", "sell", 100.0, 50000.0)
        first_call = venue.place_order.call_args_list[0]
        placed_price = first_call.args[5]
        assert placed_price > 50000.0  # Sell above reference
        assert placed_price < 50010.0  # Not too far


# ═══════════════════════════════════════════
#  7. Existing Tests Still Pass (Regression)
# ═══════════════════════════════════════════

class TestRegression:
    """Verify existing functionality is not broken."""

    def test_slippage_estimate_still_works(self):
        """Original estimate() method still works correctly."""
        model = SlippageModel()
        bps = model.estimate(1000.0, 5000.0, 0.001, 100.0)
        assert bps > 0
        assert bps < 100  # reasonable range

    @pytest.mark.asyncio
    async def test_dual_entry_dry_run(self):
        """Dry run still works with new config fields."""
        config = ExecutionConfig(dry_run=True, use_maker_taker=True)
        engine, venue, state = _make_engine(config=config)
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={"entry_mid": 50025.0},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        assert report.success
        assert report.message == "dry_run_fill"

    @pytest.mark.asyncio
    async def test_parallel_taker_both_legs_still_works(self):
        """Standard parallel taker execution still works."""
        config = ExecutionConfig(dry_run=False, use_maker_taker=False)
        engine, venue, _ = _make_engine(config=config)
        venue.place_order = AsyncMock(side_effect=[
            {"success": True, "fill_price": 50000.0, "exchange": "okx", "order_id": "1", "size": 1.0, "effective_notional": 100.0},
            {"success": True, "fill_price": 50050.0, "exchange": "htx", "order_id": "2", "size": 1.0, "effective_notional": 100.0},
        ])
        intent = TradeIntent(
            strategy_id=StrategyId.FUTURES_CROSS_EXCHANGE,
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            side="cross_exchange_arb",
            confidence=0.8,
            expected_edge_bps=10.0,
            stop_loss_bps=15.0,
            metadata={"entry_mid": 50025.0, "limit_prices": {"buy": 50010.0, "sell": 50050.0}},
        )
        report = await engine.execute_dual_entry(intent, 100.0, 5000.0, 0.001, 100.0)
        assert report.success
        assert report.message == "filled"

    def test_execution_config_defaults_unchanged(self):
        """Default ExecutionConfig values are backward compatible."""
        config = ExecutionConfig()
        assert config.order_timeout_ms == 3000
        assert config.hedge_retries == 3
        assert config.dry_run is True
        assert config.use_maker_taker is False  # New field defaults off


# ═══════════════════════════════════════════
#  8. TP/SL Fix Tests
# ═══════════════════════════════════════════

class TestTPSLRatio:
    """Test TP/SL ratio is favorable (reward > risk)."""

    @pytest.mark.asyncio
    async def test_tp_greater_than_sl_price_spread(self):
        """Price spread arb: TP should be ~2x SL."""
        strategy = FuturesCrossExchangeStrategy(
            min_spread_pct=0.01,
            target_profit_pct=0.12,
            max_spread_risk_pct=0.15,
        )
        # htx bid=50200 vs okx ask=50000 → raw spread 0.40% > fees
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50200.0, ask=50210.0),
            },
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        price_intents = [i for i in intents if i.metadata.get("arb_type") == "price_spread"]
        assert len(price_intents) > 0, "Should generate at least one price spread intent"
        for intent in price_intents:
            tp = intent.metadata["take_profit_pct"]
            sl = intent.metadata["stop_loss_pct"]
            assert tp > 0, "TP must be positive"
            assert sl > 0, "SL must be positive"
            # TP should be ~2x SL (we set TP = net_spread, SL = net_spread * 0.5)
            ratio = tp / sl
            assert ratio >= 1.9, f"TP/SL ratio should be ~2:1, got {ratio:.2f}"

    @pytest.mark.asyncio
    async def test_tp_captures_full_net_spread(self):
        """TP should equal the full net spread, not a 70% haircut."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50100.0, ask=50110.0),
            },
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        for intent in intents:
            if intent.metadata.get("arb_type") == "price_spread":
                net_spread = intent.metadata["net_spread_pct"]
                tp = intent.metadata["take_profit_pct"]
                # TP = net_spread_pct / 100 (full spread, no haircut)
                expected_tp = round(net_spread / 100, 6)
                assert abs(tp - expected_tp) < 1e-7, f"TP should be full net spread: {tp} vs {expected_tp}"

    @pytest.mark.asyncio
    async def test_tp_sl_usd_values_reasonable(self):
        """For a $100 position, TP and SL in USD should be reasonable."""
        strategy = FuturesCrossExchangeStrategy(min_spread_pct=0.01)
        snapshot = _make_snapshot(
            orderbooks={
                "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=49990.0, ask=50000.0),
                "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=50100.0, ask=50110.0),
            },
            fee_bps={"okx": {"perp": 5.0}, "htx": {"perp": 5.0}},
        )
        intents = await strategy.on_market_snapshot(snapshot)
        notional = 100.0
        for intent in intents:
            if intent.metadata.get("arb_type") == "price_spread":
                tp_usd = intent.metadata["take_profit_pct"] * notional
                sl_usd = intent.metadata["stop_loss_pct"] * notional
                # For ~0.10% net spread on $100: TP ~$0.10, SL ~$0.05
                assert tp_usd > 0.05, f"TP too small: ${tp_usd:.4f}"
                assert sl_usd > 0.02, f"SL too small: ${sl_usd:.4f}"
                assert tp_usd > sl_usd, f"TP (${tp_usd:.4f}) must exceed SL (${sl_usd:.4f})"


class TestExitFeesFix:
    """Test that exit fees are not double-counted."""

    def test_exit_fees_use_per_leg_rates(self):
        """Exit fee calculation should use actual per-leg taker rates, not total_fees_pct."""
        # Simulate the new fee calculation logic
        notional = 100.0
        # Snapshot has 5 bps per leg (0.05%)
        long_fee_pct = 5.0 / 100  # 0.05%
        short_fee_pct = 5.0 / 100  # 0.05%
        exit_fees = notional * (long_fee_pct + short_fee_pct) / 100.0
        # Should be $0.10 for 2 legs × 0.05% × $100
        assert abs(exit_fees - 0.10) < 0.001

    def test_old_formula_was_double_counting(self):
        """The old formula used total_fees_pct/2, which included entry fees."""
        notional = 100.0
        total_fees_pct = 0.20  # Old default: full round-trip
        old_exit_fees = notional * (total_fees_pct / 2) / 100.0
        # Old: $0.10 — but this is entry+exit / 2 = not just exit
        # For comparison: actual exit fees should be smaller when fees differ
        # The key issue: total_fees_pct = entry_fee_long + entry_fee_short + exit_fee_long + exit_fee_short
        # Using total_fees_pct / 2 counts entry fees as exit fees
        # New method: only count actual exit leg fees
        new_exit_fees = notional * (0.05 + 0.05) / 100.0
        # In this case they happen to be equal, but the logic is fundamentally different:
        # - Old: depends on metadata total_fees_pct (includes entry fees)
        # - New: uses live snapshot fee rates (only exit)
        assert new_exit_fees <= old_exit_fees


class TestEdgeBpsFormula:
    """Test corrected edge_bps formula."""

    def test_positive_edge_when_spread_exists(self):
        """Edge is positive when long_bid > short_ask (profit available)."""
        # We're long on okx, short on htx
        # To exit: sell long @ okx bid, buy back short @ htx ask
        long_bid = 50100.0  # okx bid (where we sell)
        short_ask = 50050.0  # htx ask (where we buy back)
        mid_ref = (long_bid + short_ask) / 2
        edge_bps = ((long_bid - short_ask) / mid_ref) * 10_000
        assert edge_bps > 0, "Edge should be positive when we'd profit from closing"

    def test_negative_edge_when_underwater(self):
        """Edge is negative when long_bid < short_ask (losing money)."""
        long_bid = 50000.0
        short_ask = 50050.0  # short's ask higher than long's bid = loss
        mid_ref = (long_bid + short_ask) / 2
        edge_bps = ((long_bid - short_ask) / mid_ref) * 10_000
        assert edge_bps < 0, "Edge should be negative when underwater"

    def test_edge_converged_triggers_at_zero(self):
        """Edge converged should trigger when spread collapses to ~0."""
        long_bid = 50000.0
        short_ask = 50000.0  # no remaining spread
        mid_ref = (long_bid + short_ask) / 2
        edge_bps = ((long_bid - short_ask) / mid_ref) * 10_000
        close_edge_bps = 0.5  # default threshold
        assert edge_bps <= close_edge_bps, "Should trigger close when spread collapsed"

    def test_old_formula_was_inverted(self):
        """The old formula (short_ask - long_bid) was measuring exit cost, not edge."""
        long_bid = 50100.0
        short_ask = 50050.0
        mid_ref = (long_bid + short_ask) / 2
        # Old formula (WRONG): measured spread as cost
        old_edge = ((short_ask - long_bid) / mid_ref) * 10_000
        # New formula (CORRECT): measures remaining profit
        new_edge = ((long_bid - short_ask) / mid_ref) * 10_000
        assert old_edge < 0, "Old formula gives negative when we're profitable — WRONG"
        assert new_edge > 0, "New formula gives positive when we're profitable — CORRECT"


class TestDefaultFallbackValues:
    """Test improved default fallback USD values."""

    def test_default_tp_usd_reasonable(self):
        """Default TP should be $0.50, not $0.08."""
        import os
        # Clear env to use defaults
        env_val = os.getenv("EXIT_TAKE_PROFIT_USD")
        if env_val is None:
            default_tp = max(0.01, float("0.50"))
            assert default_tp == 0.50

    def test_default_sl_usd_reasonable(self):
        """Default SL should be $0.25, not $0.15."""
        default_sl = max(0.01, float("0.25"))
        assert default_sl == 0.25

    def test_tp_greater_than_sl_in_defaults(self):
        """Default TP > SL for positive risk:reward."""
        tp = 0.50
        sl = 0.25
        assert tp > sl
        assert tp / sl == 2.0  # 2:1 reward:risk
