import asyncio
from arbitrage.exchanges.okx_rest import OKXRestClient
from arbitrage.exchanges.htx_rest import HTXRestClient
from arbitrage.utils.config import ExchangeConfig
from dotenv import load_dotenv
import os

load_dotenv()

async def main():
    okx_cfg = ExchangeConfig(api_key=os.getenv('OKX_API_KEY',''), api_secret=os.getenv('OKX_SECRET',''), passphrase=os.getenv('OKX_PASSPHRASE',''))
    htx_cfg = ExchangeConfig(api_key=os.getenv('HTX_API_KEY',''), api_secret=os.getenv('HTX_SECRET',''))
    okx = OKXRestClient(okx_cfg)
    htx = HTXRestClient(htx_cfg)

    print('=== OKX POSITIONS ===')
    r = await okx._request('GET', '/api/v5/account/positions', {'instType': 'SWAP'})
    if r.get('code') == '0':
        for p in r.get('data', []):
            pos = float(p.get('pos', 0))
            if pos != 0:
                inst = p['instId']
                avg = p.get('avgPx', '?')
                upl = p.get('upl', '?')
                last = p.get('last', '?')
                print(f'  {inst}: pos={pos} avgPx={avg} last={last} upl={upl}')
        if not any(float(p.get('pos',0)) != 0 for p in r.get('data',[])):
            print('  No positions')

    print('=== HTX POSITIONS ===')
    r2 = await htx._request('POST', 'https://api.hbdm.com', '/linear-swap-api/v1/swap_cross_position_info', data={'margin_account': 'USDT'})
    if r2.get('status') == 'ok':
        found = False
        for p in r2.get('data', []):
            vol = float(p.get('volume', 0))
            if vol > 0:
                found = True
                cc = p['contract_code']
                d = p.get('direction', '?')
                co = p.get('cost_open', '?')
                lp = p.get('last_price', '?')
                pnl = p.get('profit_unreal', '?')
                print(f'  {cc}: vol={vol} dir={d} cost_open={co} last={lp} pnl={pnl}')
        if not found:
            print('  No positions')

    print('=== OKX BALANCE ===')
    bal = await okx._request('GET', '/api/v5/account/balance')
    if bal.get('code') == '0':
        for d in bal['data'][0].get('details', []):
            if d.get('ccy') == 'USDT':
                avail = float(d.get('availBal', 0))
                frozen = float(d.get('frozenBal', 0))
                print(f'  avail=${avail:.4f} frozen=${frozen:.4f}')

    print('=== HTX BALANCE ===')
    r3 = await htx._request('POST', 'https://api.hbdm.com', '/linear-swap-api/v3/unified_account_info', data={'margin_account': 'USDT'})
    if r3.get('code') == 200 and r3.get('data'):
        for item in r3['data']:
            if item.get('margin_asset') == 'USDT':
                total = float(item.get('margin_balance', 0))
                avail = float(item.get('withdraw_available', 0))
                frozen = float(item.get('margin_frozen', 0))
                print(f'  total=${total:.4f} avail=${avail:.4f} frozen=${frozen:.4f}')

    await okx.close()
    await htx.close()

asyncio.run(main())
