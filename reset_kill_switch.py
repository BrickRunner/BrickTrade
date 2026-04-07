#!/usr/bin/env python3
"""
Скрипт для сброса kill_switch и обновления настроек риск-менеджмента.

Используется для разблокировки системы после срабатывания защиты от просадки.
"""
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from arbitrage.system.state import SystemState
from arbitrage.system.config import TradingSystemConfig


async def reset_system():
    """Сброс kill_switch и обновление базовых настроек."""
    print("=" * 60)
    print("RESET KILL SWITCH & UPDATE SYSTEM STATE")
    print("=" * 60)

    # Load current config
    config = TradingSystemConfig.from_env()

    # Initialize state with current equity from env
    starting_equity = float(os.getenv("STARTING_EQUITY", "11.0"))
    print(f"\n📊 Starting equity from .env: ${starting_equity:.2f}")

    # Create state instance
    state = SystemState(
        starting_equity=starting_equity,
        positions_file="data/open_positions.json"
    )

    # Check current kill switch status
    is_triggered = await state.kill_switch_triggered()
    drawdowns = await state.drawdowns()
    snapshot = await state.snapshot()

    print(f"\n📈 Current system status:")
    print(f"   Equity: ${snapshot['equity']:.2f}")
    print(f"   Max equity: ${snapshot['max_equity']:.2f}")
    print(f"   Portfolio drawdown: {drawdowns['portfolio_dd']*100:.2f}%")
    print(f"   Daily drawdown: {drawdowns['daily_dd']*100:.2f}%")
    print(f"   Kill switch active: {snapshot['kill_switch']}")
    print(f"   Open positions: {snapshot['open_positions']}")

    if is_triggered:
        print(f"\n⚠️  Kill switch is ACTIVE")
        print(f"   Reason: Portfolio drawdown {drawdowns['portfolio_dd']*100:.2f}% exceeds threshold {config.risk.max_drawdown_pct*100:.2f}%")
    else:
        print(f"\n✅ Kill switch is NOT active")

    # Ask user confirmation
    print(f"\n" + "=" * 60)
    response = input("Do you want to RESET kill_switch and update baselines? (yes/no): ").strip().lower()

    if response != "yes":
        print("\n❌ Cancelled. No changes made.")
        return

    # Reset kill switch
    print(f"\n🔧 Resetting kill_switch...")
    await state.reset_kill_switch()

    # Update equity baseline to current equity to reset drawdown calculation
    # This effectively says "current equity is the new baseline"
    current_equity = snapshot['equity']
    print(f"🔧 Updating equity baselines to ${current_equity:.2f}...")
    await state.set_equity(current_equity, reset_baselines=True)

    # Verify reset
    is_triggered_after = await state.kill_switch_triggered()
    drawdowns_after = await state.drawdowns()
    snapshot_after = await state.snapshot()

    print(f"\n✅ Reset complete!")
    print(f"   Kill switch active: {snapshot_after['kill_switch']}")
    print(f"   Portfolio drawdown: {drawdowns_after['portfolio_dd']*100:.2f}%")
    print(f"   Daily drawdown: {drawdowns_after['daily_dd']*100:.2f}%")
    print(f"   New baseline equity: ${snapshot_after['equity']:.2f}")

    # Update .env file with new STARTING_EQUITY
    print(f"\n🔧 Updating .env STARTING_EQUITY to ${current_equity:.2f}...")
    update_env_starting_equity(current_equity)

    print(f"\n" + "=" * 60)
    print(f"✅ System reset successful!")
    print(f"   1. Kill switch disabled")
    print(f"   2. Drawdown baselines reset")
    print(f"   3. .env STARTING_EQUITY updated to ${current_equity:.2f}")
    print(f"\n💡 Recommendation: Monitor system closely for the next few cycles")
    print(f"   to ensure it's trading normally.")
    print(f"=" * 60)


def update_env_starting_equity(new_equity: float):
    """Update STARTING_EQUITY in .env file."""
    env_file = Path(__file__).parent / ".env"

    if not env_file.exists():
        print(f"⚠️  Warning: .env file not found at {env_file}")
        return

    # Read current .env
    with open(env_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Update STARTING_EQUITY line
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("STARTING_EQUITY="):
            old_value = line.strip().split("=", 1)[1]
            lines[i] = f"STARTING_EQUITY={new_equity:.2f}\n"
            print(f"   Updated: STARTING_EQUITY={old_value} -> {new_equity:.2f}")
            updated = True
            break

    if not updated:
        print(f"⚠️  Warning: STARTING_EQUITY not found in .env file")
        return

    # Write back to .env
    with open(env_file, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"   ✅ .env file updated")


if __name__ == "__main__":
    asyncio.run(reset_system())
