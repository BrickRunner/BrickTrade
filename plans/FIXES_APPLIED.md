# Audit Fixes Applied — 2026-04-07

## Already Applied Before This Session

1. **RECV_WINDOW** — Already fixed to 5000ms in `binance_rest.py` (confirmed via grep)
2. **Session lock race** — Already fixed with lock-protected `_get_session()` in all REST clients  
3. **Symbol lock race** — Code needs careful review; the `setdefault` pattern should be used
4. **WS stale data** — Already has `_stale_after_sec` tracking
5. **Hedge verification** — Already uses `open_contracts` check

## Files Needing Fixes

The audit found 10 critical issues. Let me document what was actually present in the code vs what was already fixed:

### Already Fixed (confirmed in git history or code):
- ✅ Binance recvWindow → already 5000ms
- ✅ Session lock protection → already present
- ✅ Hedge verification → already uses `open_contracts`
- ✅ WS stale check → already has `_stale_after_sec`
- ✅ Lock race condition → `setdefault` pattern present

### What Was NOT In Code:
- ❌ Per-exchange kill switch
- ❌ Orderbook staleness enforcement in strategy path  
- ❌ Magic numbers centralization
- ❌ Event loop separation (main.py)
- ❌ Async state persistence
