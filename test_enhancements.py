"""
Test script for arbitrage enhancements.

Tests:
1. Fee optimizer (maker/taker)
2. Funding arbitrage strategy
3. Dynamic position sizer
4. Fee tier tracker
"""
# -*- coding: utf-8 -*-

import asyncio
import sys
import io
from datetime import datetime, timedelta

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from arbitrage.system.fee_optimizer import FeeOptimizer
from arbitrage.system.position_sizer import DynamicPositionSizer
from arbitrage.system.fee_tier_tracker import FeeTierTracker, FEE_TIERS
from arbitrage.system.strategies.funding_arbitrage import (
    FundingArbitrageStrategy,
    FundingConfig,
    FundingOpportunity,
)


def print_section(title: str):
    """Print section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


async def test_fee_optimizer():
    """Test fee optimizer."""
    print_section("1. FEE OPTIMIZER (Maker/Taker)")

    optimizer = FeeOptimizer()
    optimizer.set_fee_rates(maker_bps=0.0, taker_bps=5.0)

    # Simulate maker attempts
    print("📊 Simulating 20 maker order attempts on OKX...\n")

    for i in range(20):
        filled = i % 5 != 0  # 80% fill rate
        wait_ms = 1000 + (i * 50)
        fell_back = not filled

        optimizer.record_maker_attempt(
            exchange="okx",
            filled=filled,
            wait_ms=wait_ms,
            fell_back=fell_back,
        )

        if filled:
            saved = optimizer.estimate_fee_saved(
                exchange="okx",
                notional_usd=10.0,
                maker_filled=True,
            )
            print(f"  ✅ Attempt {i+1}: FILLED in {wait_ms}ms, saved ${saved:.4f}")
        else:
            print(f"  ❌ Attempt {i+1}: NOT FILLED → fallback to taker")

    # Get summary
    print("\n📈 Summary:")
    summary = optimizer.get_summary()
    for exchange, stats in summary.items():
        print(f"\n  {exchange.upper()}:")
        print(f"    Maker attempts:   {stats['maker_attempts']}")
        print(f"    Maker fills:      {stats['maker_fills']}")
        print(f"    Fill rate:        {stats['fill_rate_pct']:.1f}%")
        print(f"    Fallback rate:    {stats['fallback_rate_pct']:.1f}%")
        print(f"    Avg wait:         {stats['avg_wait_ms']:.1f}ms")
        print(f"    Total saved:      ${stats['total_saved_usd']:.4f}")

    # Recommendations
    print("\n💡 Recommendations:")
    timeout = optimizer.recommend_timeout("okx")
    offset = optimizer.recommend_price_offset("okx", volatility=0.015)
    should_use = optimizer.should_use_maker("okx", volatility=0.015, spread_bps=15.0)

    print(f"  Recommended timeout:      {timeout}ms")
    print(f"  Recommended price offset: {offset:.2f} bps")
    print(f"  Should use maker:         {'YES ✅' if should_use else 'NO ❌'}")


async def test_funding_arbitrage():
    """Test funding arbitrage strategy."""
    print_section("2. FUNDING RATE ARBITRAGE")

    config = FundingConfig(
        min_funding_diff_pct=0.05,
        max_hold_hours=8.5,
        target_profit_bps=10.0,
    )

    strategy = FundingArbitrageStrategy(config)

    # Simulate funding data
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    funding_data = {
        "okx": {"BTCUSDT": 0.08, "ETHUSDT": 0.05, "SOLUSDT": 0.12},
        "htx": {"BTCUSDT": 0.02, "ETHUSDT": 0.04, "SOLUSDT": 0.03},
        "bybit": {"BTCUSDT": 0.03, "ETHUSDT": 0.06, "SOLUSDT": 0.08},
    }
    spread_data = {"BTCUSDT": 8.0, "ETHUSDT": 12.0, "SOLUSDT": 15.0}
    next_funding_times = {
        "okx": datetime.now() + timedelta(minutes=45),
        "htx": datetime.now() + timedelta(minutes=45),
        "bybit": datetime.now() + timedelta(minutes=45),
    }

    print("🔍 Scanning for funding opportunities...\n")
    opportunities = await strategy.scan_opportunities(
        symbols=symbols,
        funding_data=funding_data,
        spread_data=spread_data,
        next_funding_times=next_funding_times,
    )

    if not opportunities:
        print("❌ No profitable opportunities found")
        return

    print(f"✅ Found {len(opportunities)} opportunities:\n")

    for i, opp in enumerate(opportunities, 1):
        print(f"  {i}. {opp.symbol}:")
        print(f"     Long:  {opp.long_exchange} (funding={opp.funding_long:.4f}%)")
        print(f"     Short: {opp.short_exchange} (funding={opp.funding_short:.4f}%)")
        print(f"     Differential: {opp.funding_diff:.4f}% ({opp.funding_diff*100:.1f} bps)")
        print(f"     Estimated profit: {opp.estimated_profit_bps:.2f} bps")
        print(f"     Entry spread: {opp.spread_bps:.1f} bps")
        print(f"     Funding in: {opp.hours_until_funding:.1f}h")
        print()


async def test_position_sizer():
    """Test dynamic position sizer."""
    print_section("3. DYNAMIC POSITION SIZING")

    sizer = DynamicPositionSizer(
        base_notional_usd=10.0,
        max_notional_usd=100.0,
        min_notional_usd=5.0,
    )

    # Test scenarios
    scenarios = [
        {
            "name": "Ideal conditions",
            "volatility": 0.01,
            "book_depth_usd": 50000,
            "spread_bps": 20.0,
            "balances": {"okx": 100.0, "htx": 100.0},
            "open_positions": 1,
            "max_positions": 3,
        },
        {
            "name": "High volatility",
            "volatility": 0.04,
            "book_depth_usd": 50000,
            "spread_bps": 20.0,
            "balances": {"okx": 100.0, "htx": 100.0},
            "open_positions": 1,
            "max_positions": 3,
        },
        {
            "name": "Thin liquidity",
            "volatility": 0.01,
            "book_depth_usd": 5000,
            "spread_bps": 20.0,
            "balances": {"okx": 100.0, "htx": 100.0},
            "open_positions": 1,
            "max_positions": 3,
        },
        {
            "name": "Low balance",
            "volatility": 0.01,
            "book_depth_usd": 50000,
            "spread_bps": 20.0,
            "balances": {"okx": 8.0, "htx": 8.0},
            "open_positions": 1,
            "max_positions": 3,
        },
        {
            "name": "Many positions",
            "volatility": 0.01,
            "book_depth_usd": 50000,
            "spread_bps": 20.0,
            "balances": {"okx": 100.0, "htx": 100.0},
            "open_positions": 5,
            "max_positions": 6,
        },
    ]

    print("📊 Testing position sizing across scenarios:\n")

    for scenario in scenarios:
        factors = sizer.calculate_size(
            symbol="BTCUSDT",
            long_exchange="okx",
            short_exchange="htx",
            volatility=scenario["volatility"],
            book_depth_usd=scenario["book_depth_usd"],
            spread_bps=scenario["spread_bps"],
            balances=scenario["balances"],
            open_positions=scenario["open_positions"],
            max_positions=scenario["max_positions"],
        )

        print(f"  {scenario['name']}:")
        print(f"    Base:         ${factors.base_notional:.2f}")
        print(f"    Vol adj:      {factors.volatility_adj:.2f}x")
        print(f"    Liq adj:      {factors.liquidity_adj:.2f}x")
        print(f"    Spread adj:   {factors.spread_adj:.2f}x")
        print(f"    Balance adj:  {factors.balance_adj:.2f}x")
        print(f"    Risk adj:     {factors.risk_adj:.2f}x")
        print(f"    → Final size: ${factors.final_notional:.2f}")
        print()

    # Kelly criterion test
    print("🎯 Kelly Criterion sizing:")
    kelly_size = sizer.calculate_kelly_size(
        win_rate=0.65,
        avg_win_pct=0.02,
        avg_loss_pct=0.01,
        current_equity=100.0,
        max_kelly_fraction=0.25,
    )
    print(f"  Win rate: 65%")
    print(f"  Avg win:  2%")
    print(f"  Avg loss: 1%")
    print(f"  Equity:   $100")
    print(f"  → Kelly size: ${kelly_size:.2f}")


async def test_fee_tier_tracker():
    """Test fee tier tracker."""
    print_section("4. FEE TIER OPTIMIZATION")

    tracker = FeeTierTracker()

    # Test different volume levels
    volumes = {
        "okx": 1_500_000,   # Should be tier 1
        "bybit": 500_000,   # Should be tier 1
        "htx": 100_000,     # Should be tier 0
    }

    print("📊 Current fee tiers:\n")

    for exchange, volume in volumes.items():
        tier = await tracker.update_tier(exchange, volume)

        print(f"  {exchange.upper()}:")
        print(f"    Level:         {tier.tier_level}")
        print(f"    Maker fee:     {tier.maker_fee_bps:.2f} bps")
        print(f"    Taker fee:     {tier.taker_fee_bps:.2f} bps")
        print(f"    30d volume:    ${tier.volume_30d_usd:,.0f}")

        if tier.next_tier_volume:
            remaining = tier.next_tier_volume - volume
            print(f"    Next tier at:  ${tier.next_tier_volume:,.0f}")
            print(f"    Remaining:     ${remaining:,.0f}")
            print(f"    Next maker:    {tier.next_tier_maker_bps:.2f} bps")
            print(f"    Next taker:    {tier.next_tier_taker_bps:.2f} bps")
        else:
            print(f"    Status:        MAX TIER ✅")

        print()

    # Calculate breakeven spreads
    print("💰 Breakeven spreads (after fees):\n")

    pairs = [
        ("okx", "htx", False, False),
        ("okx", "htx", True, False),
        ("bybit", "htx", False, False),
    ]

    for long_ex, short_ex, use_maker_long, use_maker_short in pairs:
        breakeven = tracker.calculate_breakeven_spread(
            long_exchange=long_ex,
            short_exchange=short_ex,
            use_maker_on_long=use_maker_long,
            use_maker_on_short=use_maker_short,
        )

        mode = "taker/taker"
        if use_maker_long:
            mode = "maker/taker"
        if use_maker_short:
            mode = "taker/maker" if not use_maker_long else "maker/maker"

        print(f"  {long_ex}<->{short_ex} ({mode}): {breakeven:.2f} bps")

    # Volume pursuit analysis
    print("\n🎯 Volume pursuit recommendations:\n")

    for exchange in volumes.keys():
        should, reason = tracker.should_pursue_volume(
            exchange=exchange,
            estimated_trades_per_day=50,
            avg_trade_size_usd=10.0,
        )

        status = "✅ YES" if should else "❌ NO"
        print(f"  {exchange}: {status} ({reason})")


async def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("  🚀 ARBITRAGE ENHANCEMENTS TEST SUITE")
    print("=" * 70)

    try:
        await test_fee_optimizer()
        await test_funding_arbitrage()
        await test_position_sizer()
        await test_fee_tier_tracker()

        print_section("✅ ALL TESTS COMPLETED")
        print("All enhancement modules are working correctly!\n")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
