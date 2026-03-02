"""Закрыть открытые позиции на HTX"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from arbitrage import ArbitrageConfig
from arbitrage.exchanges import HTXRestClient

async def main():
    config = ArbitrageConfig.from_env()
    htx = HTXRestClient(config.get_htx_config())

    # Получить позиции
    pos = await htx.get_positions()
    if pos.get("status") != "ok" or not pos.get("data"):
        print("No open positions")
        if htx.session:
            await htx.session.close()
        return

    for p in pos["data"]:
        symbol = p.get("contract_code", "?")
        direction = p.get("direction", "?")
        volume = int(float(p.get("volume", 0)))

        if volume <= 0:
            continue

        # Для закрытия long нужен sell, для short нужен buy
        close_side = "sell" if direction == "buy" else "buy"

        print(f"Closing {symbol}: {direction} {volume}ct → {close_side} market...")

        result = await htx.place_order(
            symbol=symbol,
            side=close_side,
            size=volume,
            order_type="opponent",  # market order
            offset="close",
            lever_rate=1
        )

        if result.get("status") == "ok":
            print(f"  OK: order_id={result['data'].get('order_id')}")
        else:
            err_code = result.get("err_code", result.get("err-code", "?"))
            err_msg = result.get("err_msg", result.get("err-msg", "?"))
            print(f"  FAILED: {err_code} {err_msg}")

    # Проверяем баланс после закрытия
    await asyncio.sleep(1)
    bal = await htx.get_balance()
    if bal.get("code") == 200:
        for item in bal["data"]:
            if item.get("margin_asset") == "USDT":
                print(f"\nAfter close: margin_balance=${item.get('margin_balance')}, "
                      f"withdraw_available=${item.get('withdraw_available')}, "
                      f"margin_frozen=${item.get('margin_frozen')}")

    if htx.session:
        await htx.session.close()

asyncio.run(main())
