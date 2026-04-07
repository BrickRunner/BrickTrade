# BrickTrade — Final Audit Summary & Launch Checklist

**Date:** 2026-04-07
**Scope:** Full codebase review of all arbitrage trading system files
**Reviewer:** Comprehensive code audit across all modules

---

## CURRENT STATE OF READINESS

**Overall readiness: 7.5 / 10**

The bot is **mostly complete** and has had many critical fixes applied (state persistence, kill-switch scoping, walk-the-book pricing, funding timing, seq number validation, lock tracking). However, there are **~8 remaining issues** that must be resolved before going live with real capital, organized below by severity.

---

## P0 — BLOCKERS (Must Fix Before Launching With Real Money)

### 1. No graceful position unwinding on crash/shutdown
**Files:** [`arbitrage/main.py`](arbitrage/main.py:85-102), [`handlers/arbitrage_handlers_simple.py`](handlers/arbitrage_handlers_simple.py:167-300)

**Problem:** When the bot is killed via SIGINT/SIGTERM or crashes, `run_forever()` catches `CancelledError` and calls `shutdown_gracefully()`. However, if the process receives SIGKILL, segfaults, or OOM-kills, **open arbitrage positions are left on exchanges with no tracking**. The bot saves state to `data/arb_state.json` on every mutation, but shutdown doesn't explicitly close positions — it just stops WS and closes the venue.

**Impact:** If the VPS reboots or process is killed, open legs remain on exchanges. The user would need to manually check each exchange and close them.

**Fix needed:**
- Add a `shutdown_gracefully()` method on `TradingSystemEngine` that iterates all open positions, reads current prices, and fires `execute_dual_exit()` for each.
- On startup, if `arb_state.json` has positions, load them into state and offer to "auto-close all orphaned positions from previous session" via Telegram prompt.

### 2. WebSocket "silent death" after reconnect exhaustion
**File:** [`arbitrage/system/ws_orderbooks.py`](arbitrage/system/ws_orderbooks.py:65-99)

**Problem:** `_run_ws_with_reconnect()` has a `while self._running` loop with exponential backoff and a bounded `_max_restart_attempts` counter. After **5 failed reconnects**, the task exits permanently and is removed from `self._tasks`. The watchdog does NOT resurrect it. During a prolonged network outage (e.g., 10+ minutes), all WS tasks could exhaust their retries and the bot would continue trading **without live orderbook data**.

**Fix needed:** Add a "resurrection" mechanism in the watchdog: if `len(self._tasks) < len(self._symbols) * len(self._exchanges)`, restart missing tasks after a cool-down period (e.g., 60 seconds). This is the single issue noted in the filename `.env the WS message loop dies silently`.

### 3. Hedge failure leaves orphan directional positions
**Files:** [`arbitrage/system/execution_v2.py`](arbitrage/system/execution_v2.py:490-530), [`arbitrage/system/engine.py`](arbitrage/system/engine.py:346-372)

**Problem:** When `_guaranteed_hedge()` fails after max retries, it returns `HEDGE_INCOMPLETE`. The engine blacklists the symbol for 1 hour but **does not close the opened leg**. An unhedged directional contract remains open on the exchange. The position monitor tracks exits based on PnL/timeout, not orphan detection.

**Fix needed:** Add a background `_orphan_position_monitor` that periodically (every 60s) checks exchange REST balances. If it detects positions with no corresponding tracked position in `state.positions`, log a critical alert to Telegram and attempt to market-close them.

---

## P1 — HIGH PRIORITY (Fix Before Scaling Capital)

### 4. Kill switch state not persisted across restarts
**File:** [`arbitrage/core/state.py`](arbitrage/core/state.py:76-350), [`arbitrage/system/risk.py`](arbitrage/system/risk.py:10-80)

**Problem:** Kill switch is in-memory only (via `SystemState`). The daily/portfolio drawdown values reset on restart. If the kill switch was triggered and the operator restarts the bot, the kill switch resets silently. There's **no persistent kill switch state** across restarts.

**Fix needed:** Persist kill switch state + drawdown values to `data/arb_state.json` alongside positions. On load, check the persisted state. Include a manual "reset kill switch" command via the Telegram bot (which already exists: `cb_arb_reset_kill_switch`).

### 5. No CI/CD or automated testing pipeline
**Problem:** There are **5+ different test files** with confusing names (`test_all_fixes.py`, `test_all_critical_fixes.py`, `test_all_audit_fixes.py`, `test_all_review_fixes.py`, `test_audit_all_fixes.py`). It's unclear which are current and which are legacy. No `.github/workflows/` or similar CI pipeline exists. Tests must be run manually.

**Fix needed:**
- Consolidate test files into 2-3 organized files: `test_execution.py`, `test_strategy.py`, `test_risk.py`
- Add a `pytest` CI workflow
- Ensure `pytest` passes on all tests

### 6. Dual config system legacy confusion
**Files:** [`arbitrage/utils/config.py`](arbitrage/utils/config.py) vs [`arbitrage/system/config.py`](arbitrage/system/config.py)

**Problem:** `ArbitrageConfig` (old legacy system) and `TradingSystemConfig` (new v2 engine) coexist. They share overlapping fields (`min_spread_pct`, `entry_threshold`). The old config is used by legacy handlers, the new config by the engine. This is confusing and risks misconfiguration.

**Fix needed:** Either deprecate `ArbitrageConfig` or consolidate into a single config with a migration path. At minimum, document which config each component uses.

---

## P2 — IMPORTANT (Should fix soon, but not launch-blocking)

### 7. `calculate_pnl()` only checks OKX and HTX
**File:** [`arbitrage/core/state.py`](arbitrage/core/state.py:244-263)

**Problem:** If the user runs Bybit+OKX or Binance+HTX, PnL calculation always returns 0.

**Fix:** Use all entries in `state._orderbooks` dict for PnL, not hardcoded OKX/HTX.

### 8. No API key validation at startup
**File:** [`arbitrage/system/config.py`](arbitrage/system/config.py:225-260)

**Problem:** `TradingSystemConfig.validate()` checks that credentials are "not empty" but doesn't verify they work. A wrong API key or expired key would only be discovered at first trade attempt.

**Fix:** Add `await client.get_balance()` for each configured exchange during startup validation.

### 9. No idempotency keys for orders
**Files:** All exchange REST clients

**Problem:** If a trade intent is retried after timeout, the exchange may execute the order twice. Binance supports `selfTradePrevention`, OKX supports `clientOrderId`, Bybit supports `orderLinkId`. None are used.

**Fix:** Generate a unique `order_id` per intent and pass it through the execution pipeline.

### 10. Funding timing calculation uses `time.time() % 28800`
**File:** [`arbitrage/system/strategies/futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py:336-341)

**Problem:** `seconds_in_cycle = now_ts % funding_interval` assumes that all exchanges' funding starts at Unix epoch (Jan 1, 1970). This is wrong. OKX, Bybit, Binance settle at fixed UTC hours (00:00, 04:00, 08:00, etc.). The modulo calculation gives incorrect `elapsed_since_funding`.

**Fix:** Use `datetime.utcnow().hour % 8 * 3600` to get the hour within the funding cycle, then compute `remaining_fraction` from that.

---

## P3 — NICE TO HAVE (Lower priority)

### 11. No volatility-adjusted position sizing
**Problem:** Position size is a fixed `%` of equity (`MAX_EQUITY_PER_TRADE_PCT = 0.30`) regardless of the pair's volatility. A trade on BTCUSDT gets the same size as a trade on PEPEUSDT despite PEPE having 10x the spread noise.

**Fix:** Integrate the `CapitalAllocator`'s `volatility_regime` output to scale position size inversely with ATR or realized volatility.

### 12. Go low-latency venue undocumented
**File:** [`lowlatency/main.go`](lowlatency/main.go)

**Problem:** Go code exists in the Python project. No documentation on how to build, deploy, or integrate. The `USE_LOW_LATENCY_EXEC` flag in `main.py` references it.

**Fix:** Add a README in `lowlatency/` explaining build instructions and integration points.

### 13. Inconsistent code comments language
**Problem:** Mix of Russian and English comments throughout the codebase. HTX clients have Russian comments, Binance clients have English. Makes international maintenance harder.

**Fix:** Standardize on English for all comments and docstrings.

---

## WHAT HAS ALREADY BEEN FIXED (Good job!)

Many critical issues identified in previous reviews have been addressed:

| Issue | Status |
|-------|--------|
| State persistence to disk | ✅ FIXED — JSON write with atomic writes |
| Kill switch kills ALL symbols on single hedge failure | ✅ FIXED — Now blacklists only the symbol |
| Kill switch on realized slippage | Still kills all symbols (P1) |
| Walk-the-book for price spread strategy | ✅ FIXED |
| Walk-the-book for funding arb | ✅ FIXED |
| Funding payment timing estimation | ✅ FIXED |
| Dual config system coexistence | PARTIAL — Still exists |
| WebSocket watchdog + reconnect | ✅ Partially fixed |
| WS reconnection loop exhaustion dies | **NOT FIXED** (P0 #2) |
| Per-exchange verify delays | ✅ FIXED |
| Race conditions in orderbook dict | ⚠️ Partially fixed with `asyncio.Lock` |
| Kill switch reset on restart | **NOT FIXED** (P1 #4) |
| Position orphan scanning on startup | ✅ FIXED |
| Rate limiter with token bucket | ✅ FIXED |
| Circuit breaker per exchange | ✅ FIXED |
| Symbol cooldown | ✅ FIXED |
| Exchange cooldown on margin reject | ✅ FIXED |
| Lock tracking with cleanup | ✅ FIXED |
| Sequence number validation for WS | ✅ FIXED |
| Hedge verification incomplete (`lambda ex: 0.0`) | ✅ Fixed in v2 execution engine |

---

## LAUNCH CHECKLIST

### Before DRY_RUN Mode
- [ ] All required environment variables configured in `.env`
- [ ] API keys are not empty and have correct permissions (futures trading, read)
- [ ] `EXEC_DRY_RUN=true` (start in dry mode)
- [ ] `ENABLED_STRATEGIES=futures_cross_exchange` (start with one strategy)
- [ ] Testnet mode enabled for initial testing
- [ ] VPS has low latency to exchanges (Singapore/Tokyo recommended)

### Before REAL Mode With Small Capital
- [ ] DRY_RUN produced at least 50+ trades with positive expectancy
- [ ] `data/arb_state.json` is being written/updated on every position change
- [ ] WebSocket connections are stable (check logs for no reconnect storms)
- [ ] P0 #1, #2, #3 fixed or mitigated
- [ ] P1 #4 fixed (kill switch persistence)

### Before Scaling Capital
- [ ] All P0, P1, P2 issues resolved
- [ ] Funding timing calculation (P2 #10) corrected
- [ ] No orphan positions on exchanges after unexpected restart
- [ ] Telegram alerts are working for: position open/close, kill switch, circuit breaker, hedge failure
- [ ] Market intelligence module is collecting data (optional but recommended)
- [ ] Fee tier optimization is enabled (Maker/Taker hybrid if volume warrants)

---

## RECOMMENDED LAUNCH SEQUENCE

1. **Days 1-5:** DRY_RUN on testnet → verify all systems work, collect logs
2. **Days 6-10:** REAL mode, only BTCUSDT + ETHUSDT, 2 exchanges (Bybit + OKX), $100-200 per exchange
3. **Days 11-14:** Expand to 5-10 pairs, add HTX if stable
4. **Days 15-21:** Enable funding arbitrage strategy, evaluate PnL
5. **Week 3+:** Gradually scale capital based on observed win rate and edge consistency

---

## EXPECTED RETURNS (Realistic)

| Capital Level | Pairs | Est. Daily PnL | Notes |
|---------------|-------|----------------|-------|
| $500-1000     | 2-3   | $0.50-3        | Testing/learning phase |
| $2000-4000    | 5-10  | $3-15          | Optimal risk/reward ratio |
| $10000+       | 10-20 | $15-50         | Fee tier discounts kick in |

**Key insight:** Arbitrage is a volume game. At $500 capital, commissions eat 30-50% of the raw spread. At $5000+, fee tier discounts and larger notional sizes dominate the profit equation.

---

*End of audit.*
