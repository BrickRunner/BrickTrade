"""Проверить открытые позиции и доступную маржу на HTX"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from arbitrage import ArbitrageConfig
from arbitrage.exchanges import HTXRestClient

async def main():
    config = ArbitrageConfig.from_env()
    htx = HTXRestClient(config.get_htx_config())

    # 1. Баланс (unified_account_info)
    print("=== HTX Balance ===")
    bal = await htx.get_balance()
    if bal.get("code") == 200 and bal.get("data"):
        for item in bal["data"]:
            print(f"  margin_asset: {item.get('margin_asset')}")
            print(f"  margin_balance: {item.get('margin_balance')}")
            print(f"  margin_available: {item.get('margin_available')}")
            print(f"  margin_frozen: {item.get('margin_frozen')}")
            print(f"  margin_position: {item.get('margin_position')}")
            print(f"  withdraw_available: {item.get('withdraw_available')}")
            print(f"  risk_rate: {item.get('risk_rate')}")
            # Печатаем все ключи
            print(f"  All keys: {list(item.keys())}")
    else:
        print(f"  Error: {bal}")

    # 2. Открытые позиции
    print("\n=== HTX Open Positions ===")
    pos = await htx.get_positions()
    if pos.get("status") == "ok" and pos.get("data"):
        for p in pos["data"]:
            symbol = p.get("contract_code", "?")
            direction = p.get("direction", "?")
            volume = p.get("volume", 0)
            margin = p.get("position_margin", 0)
            pnl = p.get("profit_unreal", 0)
            print(f"  {symbol}: {direction} {volume}ct, margin={margin}, PnL={pnl}")
    else:
        print(f"  No positions or error: {pos.get('status', '?')}")

    if htx.session:
        await htx.session.close()

asyncio.run(main())
