# Comprehensive Code Audit — Full Review by Top Trader & Systems Programmer

**Date:** 2026-04-07
**Scope:** All arbitrage trading system code — core, exchanges, engine, strategies, market intelligence, handlers, Telegram bot
**Reviewer Perspective:** Professional quantitative trader + senior systems architect

---

## TABLE OF CONTENTS

1. [Architecture & Design](#1-architecture--design)
2. [Core State Management](#2-core-state-management)
3. [Risk Management](#3-risk-management)
4. [Market Data Engine](#4-market-data-engine)
5. [Execution System](#5-execution-system)
6. [Exchange Connectors & WebSocket](#6-exchange-connectors--websocket)
7. [Private WebSocket Manager](#7-private-websocket-manager)
8. [Strategy Layer](#8-strategy-layer)
9. [Market Intelligence](#9-market-intelligence)
10. [Configuration System](#10-configuration-system)
11. [Handlers & Telegram Bot](#11-handlers--telegram-bot)
12. [Rate Limiter](#12-rate-limiter)
13. [Circuit Breaker](#13-circuit-breaker)
14. [Summary: Critical Issues](#14-summary-critical-issues)

---

## 1. ARCHITECTURE & DESIGN

### Strengths
- **Modular separation of concerns** — clean split between core (market_data, state, risk, notifications), system (engine, execution, config, strategies), exchanges, and market intelligence. This is production-grade architecture.
- **Dual-mode startup** — `APP_MODE=trading` or `APP_MODE=telegram` is a smart operational choice. Allows running the algo engine standalone or with a Telegram control plane.
- **Strategy runner abstraction** — strategies generate `TradeIntent`, engine validates and executes. Clean, extensible pattern.
- **Low-latency venue option** — Go-based execution venue (`lowlatency/main.go`) for when Python latency becomes a bottleneck. Excellent architectural foresight.
- **Atomic dual-leg execution** — first leg → wait for fill → second leg → hedge back on failure. This is the correct pattern for cross-exchange arb.

### Weaknesses
- **Two execution engines coexist** — [`execution.py`](../arbitrage/system/execution.py) (`AtomicExecutionEngine`) and [`execution_v2.py`](../arbitrage/system/execution_v2.py) (`AtomicExecutionEngineV2`) live side by side. This is confusing, increases maintenance surface, and risks divergence in bug fixes.
- **No dependency injection framework** — everything is wired manually in [`main.py`](../arbitrage/main.py) and [`factory.py`](../arbitrage/system/factory.py). Works fine for now but makes testing harder and coupling tighter.
- **Hardcoded exchange logic throughout** — exchange names ("okx", "htx", "bybit", "binance") are string-compared in dozens of places. Adding a new exchange requires touching 10+ files. This violates the Open/Closed Principle.

### Critical Issues
- **CRITICAL: `arbitrage/main.py` has no error recovery** — if `TradingSystemEngine.run_forever()` crashes, the process exits. No watchdog, no auto-restart, no health endpoint for orchestrator to detect.
- **CRITICAL: No graceful position unwinding on shutdown** — [`main.py:86`](../arbitrage/main.py:86) simply stops WS and closes venue. Open positions are left dangling with no tracking persisted to disk.

---

## 2. CORE STATE MANAGEMENT

### Strengths
- [`state.py`](../arbitrage/core/state.py) has clean dual-type support: `Position` for legacy single-leg and `ActivePosition` for cross-exchange arb. The position lifecycle key management is well-structured.
- Position removal handles both types gracefully.
- Legacy aliases (okx_balance, htx_balance) provide backward compat without breaking changes.

### Weaknesses
- **In-memory only** — no persistence layer for state. Process crash = complete state loss with open positions still on exchanges. This is a design-level risk.
- **`calculate_pnl()` hardcodes OKX/HTX** at line 246 — `self._orderbooks.get("okx")` and `self._orderbooks.get("htx")`. If using Bybit+OKX, PnL always returns 0. This is dead code in multi-exchange configs.
- Thread safety: `BotState` has no locks. If accessed from multiple asyncio tasks concurrently (which it is), dict operations on `self.positions` and `self.balances` can race in CPython due to GIL, but the compound operations (update + total recalc) are not atomic.

### Critical Issues
- **CRITICAL: [`remove_position()`](../arbitrage/core/state.py:144) has a logic bug** — it first tries `(strategy, symbol)` key, then iterates all positions looking for `Position` type. But `ActivePosition` instances stored as `(strategy, symbol)` keys are returned correctly — however the iteration fallback searches for `candidate.exchange == strategy` which conflates the `strategy` parameter with `exchange`. This is a naming/typing confusion.

---

## 3. RISK MANAGEMENT

### Strengths
- **Multi-layered**: pre-trade check → runtime monitoring → emergency close. The [`RiskManager`](../arbitrage/core/risk.py) in core and [`RiskEngine`](../arbitrage/system/risk.py) in system serve different purposes cleanly.
- **Circuit breaker pattern** with consecutive failure tracking is well-implemented.
- Drawdown limits with separate daily vs portfolio thresholds.
- Kill switch with both temporary (cooldown) and permanent modes.

### Weaknesses
- [`RiskManager.can_open_position()`](../arbitrage/core/risk.py:47) uses **hardcoded $5 min per side and $10 total**. Arbitrary numbers that break for very small accounts (<$20) and are too loose for large accounts.
- **Exposure check at line 69-72** — `total_exposure + per_side * 2 > max_exposure`. The `per_side * 2` double-counts because arb positions are hedged (only net exposure matters, not gross). This is over-conservative and blocks valid trades.
- No position sizing based on volatility — a fixed-size arb on BTC and PEPE gets the same risk treatment despite PEPE having 10x the spread noise.

### Critical Issues
- **CRITICAL: [`should_emergency_close()`](../arbitrage/core/risk.py:117) delta calculation is wrong** — dividing `size_usd / 2` for each leg assumes perfect 50/50 split, but partial fills and contract-size differences mean legs can be materially different sizes. The delta check gives false "safe" signals when both legs drifted equally.
- **CRITICAL: No max holding period risk check** — a position can sit open indefinitely if the monitor loop dies. The engine-level timeout exists but the core RiskManager has no fallback.

---

## 4. MARKET DATA ENGINE

### Strengths
- **Excellent instrument discovery** — [`_fetch_instruments()`](../arbitrage/core/market_data.py:190) correctly parses exchange-specific instrument formats and extracts contract sizes, tick sizes, min order sizes.
- **Good error tolerance** — `return_exceptions=True` on `asyncio.gather` means one exchange failing doesn't kill all updates.
- Latency tracking per exchange.
- Separate spot and futures instrument fetching.

### Weaknesses
- **Monolithic 1200-line file** — should be split by exchange (per-exchange modules). This file has grown too large to review easily.
- **No deduplication of refresh logic** — `_fetch_futures_prices()` has 4 giant if/elif blocks with similar parsing patterns. Could be abstracted with exchange-specific adapters.
- **No data validation** — if an exchange returns a price of 0.0001 for BTC (glitch), there's no sanity check. Bad data flows into strategy signals.

### Critical Issues
- **CRITICAL: [`initialize()`](../arbitrage/core/market_data.py:67) race condition** — `spot_tasks` dict comprehension uses `self.exchanges` as iteration source but keys are dict keys. If `self.exchanges` is a plain dict, iteration order is insertion-order in Python 3.7+, but the results are matched by `zip(tasks.keys(), results)`. The spot task results are awaited separately with `return_exceptions=True` but **the results are never consumed** — any spot instrument fetch exception is silently swallowed without logging.
- **CRITICAL: No re-fetch of instruments** — if instruments change (new listings, delistings), the bot won't know until restart. `initialize()` is called once at startup only.

---

## 5. EXECUTION SYSTEM

### Strengths
- **Atomic dual-leg with hedge-back** — this is the correct pattern for cross-exchange arb. First leg fills → second leg fills → if second leg fails → hedge first leg back. [`execution.py`](../arbitrage/system/execution.py) implements this well.
- **Per-exchange locking** with ABBA deadlock prevention — [`_acquire_exchange_locks()`](../arbitrage/system/execution.py:37) is textbook correct.
- **Pre-flight margin checks** before any orders — critical for preventing orphan positions.
- **Orphan position detection** before trades (Fix #8) — prevents "insufficient margin" errors from stale positions.
- **Dry-run support** — allows safe testing.
- Maker-Taker hybrid execution option for fee optimization.

### Weaknesses
- **Order type is "ioc" by default** — IOC (Immediate or Cancel) orders may not fill in thin orderbooks. No fallback to market orders for illiquid pairs.
- **Second leg retries (line 233-264)** — 3 attempts with 0.3s backoff, escalating to market orders. The 0.3s delay can be too long in fast markets where the spread closes.
- **Slippage estimation uses hardcoded `2_000_000` depth** in [`engine.py:249`](../arbitrage/system/engine.py:249) — should come from live depth data.

### Critical Issues
- **CRITICAL: [`execution.py` line 160](../arbitrage/system/execution.py:160) — `order_timeout_ms / 1000 * 4`** gives 12s timeout for first leg, but the venue's `wait_for_fill()` uses the original `order_timeout_ms` (3s). First leg place succeeds but wait_for_fill may timeout before the 12s outer timeout fires, creating confusing error paths.
- **CRITICAL: Hedge verification is incomplete** — [`_hedge_first_leg()`](../arbitrage/system/execution.py:276) returns `(hedged, hedge_verified, remaining_contracts)` but the hedge verify path calls `lambda ex: 0.0` as the position check function — meaning **hedge verification is always false**. This is a known incomplete implementation.
- **CRITICAL: No idempotency** — if the engine restarts mid-execution, there's no way to know if leg 1 was placed. Could result in double-placement or orphan positions.

---

## 6. EXCHANGE CONNECTORS & WEBSOCKET

### Strengths
- **Separate REST and WS modules** per exchange — clean separation for public data (WS orderbooks) vs private (account updates).
- **Robust WS reconnection** — all WS clients have `while self.running` reconnect loops with exponential backoff.
- **FIX #2 applied** — zombie connection detection with 30s heartbeat timeout on OKX and HTX.
- **Gzip decompression for HTX** ([`private_ws.py:374-389`](../arbitrage/exchanges/private_ws.py:374)) with dual-mode (gzip + plain text fallback) is correctly implemented.
- Timeout increased to 30s for HTX auth (FIX #8) — handles HTX's slow auth responses.

### Weaknesses
- **Binance and OKX WebSocket modules lack ping/pong heartbeat handling** — only HTX has explicit ping handler. OKX and Bybit rely on the `websockets` library's built-in ping, which may not suffice for all exchange expectations.
- **No WS reconnection state logging** — when WS dies and reconnects, there's no emit to monitoring. The exchange could be silent for minutes without anyone knowing.

### Critical Issues
- **CRITICAL: No Binance WebSocket implementation visible** — `binance_ws.py` exists in the file list but was not among the currently open tabs. If Binance WS is missing or incomplete, the system falls back to REST polling for Binance orderbooks, adding 200-500ms latency — fatal for arb.
- **CRITICAL: WebSocket message loops use `time.monotonic()` for heartbeat** but **no ping frames are sent to the server**. Some exchanges (HTX in particular) require client-initiated pings. If the server closes the connection for inactivity, the `recv()` timeout will catch it, but there will be a 30s gap.

---

## 7. PRIVATE WEBSOCKET MANAGER

### Strengths
- **Unified manager** orchestrating OKX, HTX, Bybit private channels (balances, orders, positions).
- **Real-time balance updates** — eliminates REST polling overhead and latency for balance checks.
- **Seed balances** on startup from market data as fallback before WS connects.
- **Thread-safe cached state** with simple getters.

### Weaknesses
- **No reconnection tracking exposed** — if private WS dies, the system silently falls back to stale cached balances. No alert is emitted.
- Balance cache has no TTL — if WS disconnects, the last cached balance persists indefinitely.

---

## 8. STRATEGY LAYER

### Strengths
- **Walk-the-book slippage estimation** — [`_check_price_spread()`](../arbitrage/system/strategies/futures_cross_exchange.py:117) uses `SlippageModel.walk_book()` for realistic fill prices instead of top-of-book. Critical for avoiding false signals.
- **Fee-aware entry logic** — round-trip fees (entry + exit) are correctly accounted for in net spread calculation.
- **Cooldown per direction** — prevents rapid re-entries on the same pair.
- **Confidence scoring** — scales with spread depth above threshold.

### Weaknesses
- **Estimate notional from balance at 5%** (line 138-139) — `snapshot.balances.get(long_ex, 0.0) * 0.05`. This is a guess, not the actual intended position size. Should use the allocator's proposed notional.
- **Depth check only looks at top 5 levels** (line 388) — for positions >$1000, top 5 levels may not have enough depth. The check should walk the full book to the target notional.
- **Funding arbitrage timing factor is hardcoded** — `time_since_last_funding = 0.0` at line 319 means funding timing adjustment is essentially a no-op. This gives wrong adjusted_income estimates.

### Critical Issues
- **CRITICAL: [`_check_funding_rate()`](../arbitrage/system/strategies/futures_cross_exchange.py:242) — entry prices use top-of-book for funding arb** even after the walk-the-book fix. The `limit_prices` metadata at line 363 still uses `long_ob.ask` and `short_ob.bid` instead of walked prices. This means the intent carries wrong limit prices to execution, potentially causing IOC rejections or fills at worse prices.
- **CRITICAL: No funding rate decay modeling** — funding rates can flip between entry and the next payment. The strategy assumes rates are static.

---

## 9. MARKET INTELLIGENCE

### Strengths
- **Full ML pipeline** — feature engineering, regime detection, opportunity scoring, portfolio analysis.
- **Regime model** with 5 regimes (trending, ranging, high_vol, panic, calm) — provides context-aware signals.
- **Structured logging** — cycle-level tracking with JSONL persistence.
- **MTF (multi-timeframe) candle caching** with 30min refresh — good for reducing API calls.
- **Offloads CPU-bound work to thread** via `asyncio.to_thread()` — correct async pattern.

### Weaknesses
- **Massive codebase** — 800+ lines per file, 15+ modules. Over-engineered relative to the arb engine it feeds.
- **`time_since_last_funding = 0.0`** — same issue as in strategy layer. The funding timing calculation is dead code.
- **Adaptive ML weighting** with feedback recording exists (line 161) but the actual weight update mechanism appears incomplete.

---

## 10. CONFIGURATION SYSTEM

### Strengths
- **Environment-driven config** — `TradingSystemConfig.from_env()` reads everything from env vars. Clean, deployable with Docker/K8s.
- **Helper functions** — `_as_bool()`, `_as_float()`, `_as_int()`, `_first_env()` are well-designed for defaults and fallbacks.
- **Validation** — [`validate()`](../arbitrage/system/config.py:225) checks constraints on ratios and required fields.
- **Immutable dataclasses** — `frozen=True` prevents runtime mutation of config.

### Weaknesses
- **No config hot-reloading** — changing env vars requires restart.
- **No config export** — can't dump effective config for debugging (except manually reading env).

---

## 11. HANDLERS & TELEGRAM BOT

### Strengths
- [`arbitrage_handlers_simple.py`](../handlers/arbitrage_handlers_simple.py) encapsulates engine state in [`_EngineState`](../handlers/arbitrage_handlers_simple.py:37) dataclass — clean single source of truth.
- **Telegram monitoring sink** — extends `InMemoryMonitoring` to emit events as Telegram messages. Excellent operational visibility.
- **Graceful shutdown** — cancels task, stops WS, closes venue, nullifies all references. Handles `CancelledError` correctly.
- **Rich notification formatting** — position open/close, critical errors, cooldown, hedge failures — all with Russian-localized messages.

### Weaknesses
- **`_es` is a module-level global singleton** — not ideal for testing, but acceptable for Telegram bot context.
- **No auth check on bot commands** — any user who knows the bot can send commands. Only `user_id` is used for notifications, not command authorization.
- **Compatibility aliases** — `_router`, `_router_task`, `_state`, `_exchanges` at line 107-111 are kept for backward compat with main.py shutdown. This is dead code risk.

---

## 12. RATE LIMITER

### Strengths
- **Token bucket algorithm** — clean, correct implementation with refill and burst capacity.
- **Per-exchange tracking** with sensible documented defaults.
- **Exponential backoff on 429** — `record_429()` with 1s → 2s → 4s up to 60s max.
- **Async-safe via `asyncio.Lock`** per bucket.

### Weaknesses
- **Global singleton** — `get_rate_limiter()` creates a global limiter that's never reset between tests. Makes unit tests non-deterministic.
- **Bucket uses `time.monotonic()`** — correct for elapsed time, but the `_refill()` is only called during `acquire()`. If no requests are made for a long time, the bucket refills to max, potentially allowing a burst when the exchange has a lower rate limit than documented.

---

## 13. CIRCUIT BREAKER

### Strengths
- **Simple and correct** — 5 consecutive errors → 10 minute cooldown. Per exchange tracking.
- **Cooldown expiration auto-re-enables** trading without manual intervention.
- **Status reporting** for monitoring.

### Weaknesses
- **Cooldown is hardcoded 10 minutes** — should be configurable via env.
- **No differentiation between error types** — a transient "insufficient margin" error counts the same as a connection timeout. These should have different severities.

---

## 14. ENGINE-LEVEL (engine.py) REVIEW

### Strengths
- **Main cycle loop is well-structured** — balance sync, health check, position processing, strategy selection, risk validation, execution.
- **Symbol cooldown** — prevents hammering failing symbols.
- **Underfunded exchange filtering** — smart pre-check that avoids futile intent generation.
- **Exchange cooldown on margin reject** — blocks the specific exchange for 30 minutes instead of just the symbol.

### Critical Issues
- **CRITICAL: [`engine.py:344`](../arbitrage/system/engine.py:344) — kill switch on hedge failure** — `await self.risk.state.trigger_kill_switch(permanent=False)` followed by `return`. This **kills the entire engine for ALL symbols** because one symbol's hedge failed. This is catastrophically over-broad. A single symbol's hedge failure should only blacklist that symbol, not halt trading on all other symbols.
- **CRITICAL: [`engine.py:9`](../arbitrage/system/engine.py:9) — env var cached at module load** — `_MAX_EQUITY_PER_TRADE_PCT` is cached at import time. If the user wants to change it, they must restart the process. The comment claims "respects env var at startup" but in a long-running daemon, startup was hours ago. This defeats the purpose of runtime configurability.
- **CRITICAL: [`engine.py:311`](../arbitrage/system/engine.py:311) — 300 second (5 minute) cooldown** on symbol after first-leg failure. For a symbol that repeatedly fails, this adds up. But the cooldown doesn't account for *why* it failed — if it was an exchange-side issue (maintenance), the cooldown is correct. If it was a thin orderbook, the cooldown just wastes opportunity.

---

## OVERALL ASSESSMENT

### Architecture Grade: B+
Clean separation, well-structured, but hampered by hardcoded exchange logic and missing abstraction layers.

### Risk Management Grade: B-
Good multi-layered approach but exposure calculation is wrong, delta check is broken, and no volatility-adjusted position sizing.

### Execution Quality Grade: B
Correct atomic pattern with hedge-back, but hedge verification is incomplete and idempotency is missing.

### Code Quality Grade: B-
Generally well-organized, good use of dataclasses and async patterns, but too many hardcoded values, dual execution engines, and in-memory-only state.

### Production Readiness: **7/10**
The system could run in production with careful monitoring, but the critical issues (especially state loss on crash, kill-switch over-broadness, and broken PnL calculation for non-OKX/HTX pairs) must be fixed before handling significant capital.

### Top 5 Priorities to Fix Before Scaling Capital:

1. **Persist state to disk** — open positions must survive process crash (SQLite or JSON with atomic writes)
2. **Fix kill-switch scope** — symbol-level failures must not kill the entire engine
3. **Fix `calculate_pnl()` to support all exchange pairs**, not just OKX/HTX
4. **Complete hedge verification** — currently `lambda ex: 0.0` means hedge is never verified
5. **Add graceful position unwinding on shutdown** — all open positions must be tracked and optionally closed before exit
