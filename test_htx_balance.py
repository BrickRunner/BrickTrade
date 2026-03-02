"""Тест проверки OKX account mode"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from arbitrage import ArbitrageConfig
from arbitrage.exchanges import OKXRestClient

async def main():
    config = ArbitrageConfig.from_env()
    okx = OKXRestClient(config.get_okx_config())

    result = await okx._request("GET", "/api/v5/account/config")
    if result.get("code") == "0" and result.get("data"):
        d = result["data"][0]
        acct_lv = d.get("acctLv", "?")
        perm = d.get("perm", "?")
        modes = {"1": "Simple", "2": "Single-currency", "3": "Multi-currency", "4": "Portfolio"}
        print(f"Account mode: {modes.get(acct_lv, acct_lv)} (acctLv={acct_lv})")
        print(f"API permissions: {perm}")
        if acct_lv == "1":
            print("\n❌ ПРОБЛЕМА: Simple mode НЕ поддерживает фьючерсы!")
            print("   Решение: OKX → Settings → Account mode → Single-currency margin")
        if "trade" not in perm.lower():
            print("\n❌ ПРОБЛЕМА: API ключ без торговых прав!")
            print("   Решение: Создайте новый ключ с Read + Trade")

    if hasattr(okx, 'session') and okx.session:
        await okx.session.close()

asyncio.run(main())
