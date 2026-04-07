"""
Tests for Dynamic Position Sizer.
"""
import pytest

from arbitrage.system.position_sizer import DynamicPositionSizer, SizingFactors


def test_basic_size_calculation():
    """Test basic position sizing."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0, max_notional_usd=100.0, min_notional_usd=5.0)

    factors = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,  # 1% volatility - normal
        book_depth_usd=100.0,  # Adequate depth (10x position)
        spread_bps=15.0,  # Good spread
        balances={"okx": 50.0, "bybit": 50.0},  # Sufficient balance
        open_positions=0,
        max_positions=5,
    )

    # Should be close to base notional with slight adjustments
    # Note: liquidity adj is 1.3 for very deep books (2x target depth)
    assert factors.final_notional >= 8.0
    assert factors.final_notional <= 20.0  # May go higher with very favorable conditions
    assert factors.volatility_adj == 1.2  # Low vol = increase
    assert factors.liquidity_adj >= 1.0  # Adequate depth
    assert factors.spread_adj == 1.1  # Good spread
    assert factors.balance_adj > 0
    assert factors.risk_adj == 1.0  # Low utilization


def test_high_volatility_reduces_size():
    """Test that high volatility reduces position size."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0)

    # Normal volatility
    factors_normal = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=100.0,
        spread_bps=15.0,
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=0,
        max_positions=5,
    )

    # High volatility
    factors_high = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.05,  # 5% volatility - very high
        book_depth_usd=100.0,
        spread_bps=15.0,
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=0,
        max_positions=5,
    )

    # High volatility should result in smaller position
    assert factors_high.volatility_adj < factors_normal.volatility_adj
    assert factors_high.final_notional < factors_normal.final_notional


def test_low_liquidity_reduces_size():
    """Test that low liquidity reduces position size."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0)

    # Deep book
    factors_deep = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=200.0,  # Very deep
        spread_bps=15.0,
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=0,
        max_positions=5,
    )

    # Thin book
    factors_thin = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=20.0,  # Thin
        spread_bps=15.0,
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=0,
        max_positions=5,
    )

    assert factors_thin.liquidity_adj < factors_deep.liquidity_adj
    assert factors_thin.final_notional < factors_deep.final_notional


def test_insufficient_balance_reduces_size():
    """Test that insufficient balance limits position size."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0)

    # Low balance
    factors = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=100.0,
        spread_bps=15.0,
        balances={"okx": 5.0, "bybit": 50.0},  # okx has low balance
        open_positions=0,
        max_positions=5,
    )

    # Balance should be the limiting factor
    assert factors.balance_adj < 0.5
    assert factors.final_notional < 10.0


def test_zero_balance_prevents_trade():
    """Test that zero balance results in zero position size."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0, min_notional_usd=5.0)

    factors = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=100.0,
        spread_bps=15.0,
        balances={"okx": 0.0, "bybit": 50.0},  # No balance on okx
        open_positions=0,
        max_positions=5,
    )

    assert factors.balance_adj == 0.0
    assert factors.final_notional == sizer.min_notional  # Clamped to minimum


def test_high_position_utilization_reduces_size():
    """Test that high position utilization reduces size."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0)

    # Low utilization
    factors_low = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=100.0,
        spread_bps=15.0,
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=1,
        max_positions=10,  # Only 10% utilized
    )

    # High utilization
    factors_high = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.01,
        book_depth_usd=100.0,
        spread_bps=15.0,
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=9,
        max_positions=10,  # 90% utilized
    )

    assert factors_high.risk_adj < factors_low.risk_adj
    assert factors_high.final_notional < factors_low.final_notional


def test_kelly_criterion_sizing():
    """Test Kelly Criterion position sizing."""
    sizer = DynamicPositionSizer()

    # Good win rate and ratio
    notional = sizer.calculate_kelly_size(
        win_rate=0.60,
        avg_win_pct=0.02,
        avg_loss_pct=0.01,
        current_equity=100.0,
        max_kelly_fraction=0.25,
    )

    assert notional > 0
    assert notional <= 100.0 * 0.50  # Should not exceed 50% of equity

    # Poor win rate
    notional_poor = sizer.calculate_kelly_size(
        win_rate=0.40,
        avg_win_pct=0.01,
        avg_loss_pct=0.02,
        current_equity=100.0,
    )

    # Poor stats should result in minimum notional
    assert notional_poor == sizer.min_notional


def test_correlation_adjustment():
    """Test position size adjustment for correlated instruments."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0)

    # Adding BTC when no positions
    size_first = sizer.adjust_for_correlation(10.0, [], "BTCUSDT")
    assert size_first == 10.0  # No reduction

    # Adding ETH when BTC already open
    size_correlated = sizer.adjust_for_correlation(10.0, ["BTCUSDT"], "ETHUSDT")
    assert size_correlated < 10.0  # Reduced due to correlation
    assert size_correlated == 8.0  # 20% reduction

    # Adding uncorrelated asset
    size_uncorrelated = sizer.adjust_for_correlation(10.0, ["BTCUSDT"], "LINKUSDT")
    assert size_uncorrelated == 10.0  # No reduction


def test_recommend_base_notional():
    """Test base notional recommendation."""
    sizer = DynamicPositionSizer(min_notional_usd=5.0, max_notional_usd=100.0)

    # Small account
    notional_small = sizer.recommend_base_notional(
        total_equity=100.0,
        max_positions=5,
        risk_per_trade_pct=0.02,
    )
    assert notional_small >= 5.0
    assert notional_small <= 100.0

    # Large account
    notional_large = sizer.recommend_base_notional(
        total_equity=10000.0,
        max_positions=10,
        risk_per_trade_pct=0.02,
    )
    assert notional_large >= notional_small
    assert notional_large <= 100.0  # Capped at max


def test_min_max_clamping():
    """Test that final notional is always clamped to min/max."""
    sizer = DynamicPositionSizer(base_notional_usd=10.0, min_notional_usd=5.0, max_notional_usd=20.0)

    # Test minimum clamping
    factors_min = sizer.calculate_size(
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="bybit",
        volatility=0.10,  # Extreme volatility
        book_depth_usd=5.0,  # Very thin
        spread_bps=3.0,  # Tight spread
        balances={"okx": 50.0, "bybit": 50.0},
        open_positions=0,
        max_positions=5,
    )
    assert factors_min.final_notional >= 5.0

    # Test maximum clamping (would require very favorable conditions)
    # For now, just verify it doesn't exceed max
    assert factors_min.final_notional <= 20.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
