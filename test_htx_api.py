"""
Тест HTX API endpoints
"""
import asyncio
import aiohttp

FUTURES_BASE_URL = "https://api.hbdm.com"

async def test_endpoints():
    async with aiohttp.ClientSession() as session:
        print("Testing HTX API endpoints...\n")

        # Тест 1: Contract info
        endpoints = [
            "/linear-swap-api/v1/swap_contract_info",  # v1
            "/linear-swap-api/v3/swap_contract_info",  # v3
            "/api/v1/contract_contract_info",          # старый
        ]

        print("=" * 70)
        print("ТЕСТ 1: Contract Info (список инструментов)")
        print("=" * 70)
        for endpoint in endpoints:
            try:
                url = f"{FUTURES_BASE_URL}{endpoint}"
                print(f"\n🔍 Testing: {endpoint}")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json(content_type=None)
                    print(f"   Status code: {resp.status}")
                    print(f"   Response keys: {list(data.keys())[:5]}")
                    if data.get("status") == "ok":
                        count = len(data.get("data", []))
                        print(f"   ✅ SUCCESS! Found {count} contracts")
                        # Показываем первый контракт
                        if count > 0:
                            first = data["data"][0]
                            print(f"   Sample: {first.get('contract_code', 'N/A')}")
                    else:
                        print(f"   ❌ Error: {data}")
            except Exception as e:
                print(f"   ❌ Exception: {e}")

        # Тест 2: Market tickers
        ticker_endpoints = [
            "/linear-swap-ex/market/detail/merged",       # Merged (единичный)
            "/linear-swap-ex/market/detail/batch_merged", # Batch merged
            "/linear-swap-ex/market/depth",                # Depth
            "/swap-ex/market/detail/batch_merged",         # Старый
        ]

        print("\n" + "=" * 70)
        print("ТЕСТ 2: Market Tickers (котировки)")
        print("=" * 70)
        for endpoint in ticker_endpoints:
            try:
                url = f"{FUTURES_BASE_URL}{endpoint}"
                print(f"\n🔍 Testing: {endpoint}")

                # Для некоторых endpoints нужен параметр
                params = {}
                if "batch_merged" in endpoint or "merged" in endpoint:
                    params = {"contract_code": "BTC-USDT"}

                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json(content_type=None)
                    print(f"   Status code: {resp.status}")
                    print(f"   Response keys: {list(data.keys())[:5]}")
                    if data.get("status") == "ok":
                        print(f"   ✅ SUCCESS!")
                        # Показываем структуру
                        if "tick" in data:
                            tick = data["tick"]
                            print(f"   Tick keys: {list(tick.keys())[:10]}")
                            print(f"   Bid: {tick.get('bid', tick.get('bids', 'N/A'))}")
                            print(f"   Ask: {tick.get('ask', tick.get('asks', 'N/A'))}")
                        elif "data" in data:
                            items = data["data"]
                            print(f"   Data items: {len(items) if isinstance(items, list) else 'N/A'}")
                            if isinstance(items, list) and len(items) > 0:
                                first = items[0]
                                print(f"   Sample keys: {list(first.keys())[:10]}")
                    else:
                        print(f"   ❌ Error: {data.get('err-msg', data.get('err_msg', data))}")
            except Exception as e:
                print(f"   ❌ Exception: {e}")

        print("\n" + "=" * 70)
        print("ТЕСТ ЗАВЕРШЕН")
        print("=" * 70)

if __name__ == "__main__":
    asyncio.run(test_endpoints())
