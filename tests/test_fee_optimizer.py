"""
Tests for Fee Optimizer and Fee Tier Tracker.
"""
import pytest

from arbitrage.system.fee_optimizer import FeeOptimizer, FeeStats
from arbitrage.system.fee_tier_tracker import FeeTier, FeeTierTracker


# ── FeeOptimizer Tests ──────────────────────────────────────────────


def test_fee_stats_calculations():
    """Test FeeStats calculation methods."""
    stats = FeeStats(
        maker_attempts=10,
        maker_fills=8,
        maker_fallback_to_taker=2,
        taker_only=5,
    )

    assert stats.fill_rate() == 80.0
    assert stats.fallback_rate() == 20.0

    # Edge case: no attempts
    empty_stats = FeeStats()
    assert empty_stats.fill_rate() == 0.0
    assert empty_stats.fallback_rate() == 0.0


def test_recommend_timeout():
    """Test maker timeout recommendations."""
    optimizer = FeeOptimizer()

    # Not enough data - use default
    timeout = optimizer.recommend_timeout("okx")
    assert timeout == 2000

    # Fast fills
    optimizer.get_stats("okx").maker_fills = 20
    optimizer.get_stats("okx").maker_avg_wait_ms = 300.0
    timeout_fast = optimizer.recommend_timeout("okx")
    assert timeout_fast == 1500

    # Slow fills
    optimizer.get_stats("okx").maker_avg_wait_ms = 2500.0
    timeout_slow = optimizer.recommend_timeout("okx")
    assert timeout_slow == 3000


def test_recommend_price_offset():
    """Test price offset recommendations based on fill rate."""
    optimizer = FeeOptimizer()

    # Not enough data - use default
    offset = optimizer.recommend_price_offset("okx")
    assert offset == 0.5

    # High fill rate - tighter offset
    optimizer.get_stats("okx").maker_attempts = 20
    optimizer.get_stats("okx").maker_fills = 18
    offset_high = optimizer.recommend_price_offset("okx")
    assert offset_high == 0.3

    # Low fill rate - wider offset
    optimizer.get_stats("okx").maker_fills = 5
    offset_low = optimizer.recommend_price_offset("okx")
    assert offset_low == 1.0

    # High volatility adjustment
    offset_volatile = optimizer.recommend_price_offset("okx", volatility=0.03)
    # Should widen for high volatility


def test_should_use_maker():
    """Test decision logic for using maker orders."""
    optimizer = FeeOptimizer()

    # Always try maker for first 20 attempts
    for i in range(20):
        optimizer.record_maker_attempt("okx", filled=True, wait_ms=1000.0)
        assert optimizer.should_use_maker("okx", volatility=0.01, spread_bps=15.0)

    # After 20 attempts, check conditions
    # Good fill rate, low volatility, wide spread - should use maker
    assert optimizer.should_use_maker("okx", volatility=0.01, spread_bps=15.0)

    # High volatility - should not use maker
    assert not optimizer.should_use_maker("okx", volatility=0.05, spread_bps=15.0)

    # Tight spread - should not use maker
    assert not optimizer.should_use_maker("okx", volatility=0.01, spread_bps=5.0)

    # Poor fill rate - should not use maker
    optimizer.get_stats("okx").maker_fills = 5  # 25% fill rate
    assert not optimizer.should_use_maker("okx", volatility=0.01, spread_bps=15.0)


def test_record_maker_attempt():
    """Test recording maker attempt outcomes."""
    optimizer = FeeOptimizer()

    # Record successful fill
    optimizer.record_maker_attempt("okx", filled=True, wait_ms=500.0, fell_back=False)
    stats = optimizer.get_stats("okx")
    assert stats.maker_attempts == 1
    assert stats.maker_fills == 1
    assert stats.maker_fallback_to_taker == 0
    assert stats.maker_avg_wait_ms == 500.0

    # Record another fill with different wait time
    optimizer.record_maker_attempt("okx", filled=True, wait_ms=1000.0, fell_back=False)
    stats = optimizer.get_stats("okx")
    assert stats.maker_attempts == 2
    assert stats.maker_fills == 2
    # Average should be weighted (EMA)
    assert 500.0 < stats.maker_avg_wait_ms < 1000.0

    # Record fallback
    optimizer.record_maker_attempt("okx", filled=False, wait_ms=2000.0, fell_back=True)
    stats = optimizer.get_stats("okx")
    assert stats.maker_attempts == 3
    assert stats.maker_fills == 2
    assert stats.maker_fallback_to_taker == 1


def test_estimate_fee_saved():
    """Test fee savings calculation."""
    optimizer = FeeOptimizer()
    optimizer.set_fee_rates(maker_bps=0.0, taker_bps=5.0)

    # Maker filled - should save 5 bps
    saved = optimizer.estimate_fee_saved("okx", notional_usd=100.0, maker_filled=True)
    assert saved == 100.0 * (5.0 / 10_000)  # 5 bps = 0.05 USD on 100 USD

    # Check cumulative tracking
    stats = optimizer.get_stats("okx")
    assert stats.total_fee_saved_usd == saved

    # Maker not filled - no savings
    saved_none = optimizer.estimate_fee_saved("okx", notional_usd=100.0, maker_filled=False)
    assert saved_none == 0.0


def test_get_summary():
    """Test summary generation."""
    optimizer = FeeOptimizer()

    optimizer.record_maker_attempt("okx", filled=True, wait_ms=500.0)
    optimizer.record_maker_attempt("okx", filled=True, wait_ms=600.0)
    optimizer.record_maker_attempt("okx", filled=False, wait_ms=2000.0, fell_back=True)
    optimizer.record_taker_only("okx")

    summary = optimizer.get_summary()

    assert "okx" in summary
    okx_stats = summary["okx"]
    assert okx_stats["maker_attempts"] == 3
    assert okx_stats["maker_fills"] == 2
    assert okx_stats["fill_rate_pct"] == pytest.approx(66.7, rel=0.1)
    assert okx_stats["fallback_rate_pct"] == pytest.approx(33.3, rel=0.1)
    assert okx_stats["taker_only"] == 1


# ── FeeTierTracker Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_tier_basic():
    """Test fee tier update based on volume."""
    tracker = FeeTierTracker()

    # Low volume - should be tier 0
    tier = await tracker.update_tier("okx", volume_30d_usd=100_000.0)
    assert tier.tier_level == 0
    assert tier.maker_fee_bps == 2.0
    assert tier.taker_fee_bps == 5.0

    # Higher volume - should upgrade tier
    tier = await tracker.update_tier("okx", volume_30d_usd=1_000_000.0)
    assert tier.tier_level == 1
    assert tier.maker_fee_bps == 1.5
    assert tier.taker_fee_bps == 4.0

    # Very high volume
    tier = await tracker.update_tier("okx", volume_30d_usd=60_000_000.0)
    assert tier.tier_level == 4
    assert tier.maker_fee_bps == 0.0
    assert tier.taker_fee_bps == 2.5


@pytest.mark.asyncio
async def test_update_tier_next_tier_info():
    """Test that next tier information is populated."""
    tracker = FeeTierTracker()

    tier = await tracker.update_tier("okx", volume_30d_usd=100_000.0)

    # Should have next tier info
    assert tier.next_tier_volume is not None
    assert tier.next_tier_volume > 100_000.0
    assert tier.next_tier_maker_bps is not None
    assert tier.next_tier_taker_bps is not None

    # Max tier should not have next tier
    tier_max = await tracker.update_tier("okx", volume_30d_usd=200_000_000.0)
    assert tier_max.next_tier_volume is None


def test_calculate_breakeven_spread():
    """Test breakeven spread calculation."""
    tracker = FeeTierTracker()

    # Mock tiers
    tracker.current_tiers["okx"] = FeeTier(
        exchange="okx",
        tier_level=0,
        maker_fee_bps=2.0,
        taker_fee_bps=5.0,
        volume_30d_usd=0.0,
    )
    tracker.current_tiers["bybit"] = FeeTier(
        exchange="bybit",
        tier_level=0,
        maker_fee_bps=1.0,
        taker_fee_bps=6.0,
        volume_30d_usd=0.0,
    )

    # Taker-taker execution
    breakeven_taker = tracker.calculate_breakeven_spread("okx", "bybit", use_maker_on_long=False, use_maker_on_short=False)
    # Entry: 5 + 6 = 11 bps
    # Exit: 5 + 6 = 11 bps
    # Total: 22 bps + 2 bps buffer = 24 bps
    assert breakeven_taker == 24.0

    # Maker-taker execution (maker on long)
    breakeven_maker = tracker.calculate_breakeven_spread("okx", "bybit", use_maker_on_long=True, use_maker_on_short=False)
    # Entry: 2 + 6 = 8 bps
    # Exit: 5 + 6 = 11 bps
    # Total: 19 bps + 2 bps buffer = 21 bps
    assert breakeven_maker == 21.0


def test_should_pursue_volume():
    """Test volume pursuit recommendation."""
    tracker = FeeTierTracker()

    tracker.current_tiers["okx"] = FeeTier(
        exchange="okx",
        tier_level=0,
        maker_fee_bps=2.0,
        taker_fee_bps=5.0,
        volume_30d_usd=400_000.0,
        next_tier_volume=500_000.0,
        next_tier_maker_bps=1.5,
        next_tier_taker_bps=4.0,
    )

    # High trading activity - can reach next tier quickly
    should_pursue, reason = tracker.should_pursue_volume(
        exchange="okx",
        estimated_trades_per_day=50,
        avg_trade_size_usd=100.0,
    )
    # 50 trades * 100 USD * 2 legs = 10k/day
    # Need 100k more, so 10 days to reach
    assert should_pursue
    assert "reach_in" in reason

    # Low trading activity - won't reach quickly
    should_pursue_low, reason_low = tracker.should_pursue_volume(
        exchange="okx",
        estimated_trades_per_day=1,
        avg_trade_size_usd=10.0,
    )
    # 1 trade * 10 USD * 2 legs = 20/day
    # Need 100k more, so 5000 days - not worthwhile
    assert not should_pursue_low


def test_adjust_min_spread_for_fees():
    """Test minimum spread adjustment based on fees."""
    tracker = FeeTierTracker()

    tracker.current_tiers["okx"] = FeeTier(
        exchange="okx",
        tier_level=0,
        maker_fee_bps=2.0,
        taker_fee_bps=5.0,
        volume_30d_usd=0.0,
    )
    tracker.current_tiers["bybit"] = FeeTier(
        exchange="bybit",
        tier_level=0,
        maker_fee_bps=1.0,
        taker_fee_bps=6.0,
        volume_30d_usd=0.0,
    )

    # Base threshold is 10 bps
    adjusted = tracker.adjust_min_spread_for_fees(10.0, "okx", "bybit")

    # Breakeven is 24 bps (from previous test)
    # Min profitable = 24 + 3 = 27 bps
    # Should use the higher of base (10) or adjusted (27)
    assert adjusted == 27.0

    # High base threshold
    adjusted_high = tracker.adjust_min_spread_for_fees(30.0, "okx", "bybit")
    # Should keep the higher base threshold
    assert adjusted_high == 30.0


def test_recommend_maker_usage():
    """Test maker usage recommendation."""
    tracker = FeeTierTracker()

    # Positive maker fee - don't recommend
    tracker.current_tiers["okx"] = FeeTier(
        exchange="okx",
        tier_level=0,
        maker_fee_bps=2.0,
        taker_fee_bps=5.0,
        volume_30d_usd=0.0,
    )
    assert not tracker.recommend_maker_usage("okx")

    # Very low maker fee - recommend
    tracker.current_tiers["okx"].maker_fee_bps = 0.5
    assert tracker.recommend_maker_usage("okx")

    # Maker rebate - definitely recommend
    tracker.current_tiers["okx"].maker_fee_bps = -0.5
    assert tracker.recommend_maker_usage("okx")


def test_record_trade_volume():
    """Test trade volume recording."""
    tracker = FeeTierTracker()

    tracker.record_trade_volume("okx", 100.0)
    tracker.record_trade_volume("okx", 200.0)
    tracker.record_trade_volume("bybit", 50.0)

    assert tracker._volume_tracker["okx"] == 300.0
    assert tracker._volume_tracker["bybit"] == 50.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
