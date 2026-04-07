# Code Review: Arbitrage Trading Bot
**Author: Best Trader & Senior Engineer**
**Date: 2026-04-07**

---

## 1. Architecture & Project Structure

### Strengths
- **Modular architecture**: Clear separation into `core/`, `exchanges/`, `system/`, `strategies/`, `utils/`. Each package has a well-defined responsibility boundary.
- **Interface-driven design**: [`StrategyRunner`](arbitrage/system/strategy_runner.py), [`MarketDataProvider`](arbitrage/system/interfaces.py), [`ExecutionVenue`](arbitrage/system/interfaces.py) — protocol-based interfaces allow swapping implementations (test, live, mock).
- **Dataclass-driven config**: [`TradingSystemConfig.from_env()`](arbitrage/system/config.py:154) — centralized env loading, production standard.
- **Multi-exchange support**: Unified API for OKX, HTX, Bybit, Binance — enables horizontal scaling.

### Weaknesses
- **Dual config systems coexist**: [`arbitrage/utils/config.py`](arbitrage/utils/config.py) (ArbitrageConfig) and [`arbitrage/system/config.py`](arbitrage/system/config.py) (TradingSystemConfig). This is a legacy tail: the first is for the old "legacy" system, the second for the new "v2" engine. Two independent configs with overlapping fields (min_spread_pct, entry_threshold, etc.) — **classic code smell**. Developers must know which config is used where.
- **Monorepo of two bots**: Exchange rate bot + arbitrage bot in same repo. Not critical, but increases cognitive load and mixes dependencies.
- **No DI container**: Dependencies assembled manually in `build_*` functions — acceptable now but will become painful as the system grows.

---

## 2. Execution System

### Strengths
- **Two-phase atomic scheme** ([`execution_v2.py:70`](arbitrage/system/execution_v2.py:70)): Preflight → Simultaneous entry → Verification → Hedge — correct approach for arbitrage where minimizing orphaned position risk is paramount.
- **Guaranteed hedge** ([`_guaranteed_hedge()`](arbitrage/system/execution_v2.py:490)): On partial fill, hedge immediately. This prevents directional exposure.
- **Per-exchange verify delays**: [`PER_EXCHANGE_VERIFY_DELAY`](arbitrage/system/execution_v2.py:127) — HTX=3s, OKX=2s. **This is a smart engineering trick** accounting for real-world REST position propagation latency differences across exchanges.
- **Emergency hedge on partial fill** ([`_emergency_hedge_all`](arbitrage/system/execution_v2.py:212-213)): FIX #11 correctly checks that at least one leg was filled before hedging. Prevents creating positions from nothing.

### Weaknesses
- **Code duplication across execution layers**: There's [`execution.py`](arbitrage/system/execution.py) (atomic engine v1), [`execution_v2.py`](arbitrage/system/execution_v2.py) (v2), AND [`live_adapters.py:LiveExecutionVenue`](arbitrage/system/live_adapters.py:300) — another execution layer. Three execution layers is **overengineering**.
- **No `return_exceptions=True` in critical gather() calls**: [`_execute_both_legs()`](arbitrage/system/execution_v2.py:401) correctly uses it, but [`engine.run_cycle()`](arbitrage/system/engine.py:137) lacks it — any error in `provider.get_snapshot()` could crash the cycle (mitigated by `try/except` at `run_forever` level).

### Critical Errors
- **[CRITICAL #1 — Potential silent failure in hedge]**: [`_guaranteed_hedge()`](arbitrage/system/execution_v2.py:490) retries up to `max_hedge_attempts`, but if ALL fail, it returns `HEDGE_INCOMPLETE`. The handler in [`engine.run_cycle()`](arbitrage/system/engine.py:333-344) only calls `trigger_kill_switch(permanent=False)` and returns — **but does NOT close the opened leg**. If the first leg filled and hedge failed, an unhedged directional contract remains open on the exchange. There's a background position monitor but it processes exits based on PnL/timeout, not orphan detection. `_scan_orphaned_positions()` is startup-only, not continuous.
- **[CRITICAL #2 — Race in `_verify_positions()`]**: [`execution_v2.py:462-482`](arbitrage/system/execution_v2.py:462) — retry loop 3 times with `1.0 + attempt` seconds. But if between checks the price moves and a position wasn't filled (market order on one leg was rejected), `_guaranteed_hedge()` receives e.g. `pos_a=0, pos_b=0.5` and will try to hedge 0.5 — while the first order may have already been cancelled or never executed. No `cancel_order()` is called before hedge.

---

## 3. Risk Management

### Strengths
- **Multi-layered checks**: Kill switch, latency, leverage, slippage, orderbook staleness, inventory imbalance, drawdown (daily + portfolio), exposure caps, strategy allocation — this is **serious** risk management.
- **Inventory imbalance scoped to trade exchanges** ([`risk.py:60-67`](arbitrage/system/risk.py:60)): Only the imbalance between the TWO exchanges involved in the trade is checked, not global. **This is an important and smart fix** — with 3+ exchanges, global imbalance would always be high and block all trades.
- **Circuit breaker** ([`ExchangeCircuitBreaker`](arbitrage/system/circuit_breaker.py)): Error/success recording per exchange, automatic blocking on threshold breach.
- **Symbol cooldown**: [`_symbol_cooldown_until`](arbitrage/system/engine.py:64) — noisy pairs don't flood the system with rejected intents.

### Weaknesses
- **`validate_intent` is expensive per cycle**: Each call does at least 2 `await self.state.snapshot()` calls (line 69 + line 81). Costly at `cycle_interval_seconds=0.5`.
- **Config risk defaults are contradictory**: [`RiskConfig`](arbitrage/system/config.py:44) defaults to `max_total_exposure_pct=0.30`, but `from_env()` overrides it to 0.65. Similarly `max_open_positions=3` → 20. **Two sets of defaults is very confusing.**
- **No dynamic position sizing by volatility**: No Kelly Criterion or volatility-targeting. Position size is a fixed % of equity regardless of the pair's volatility.

### Critical Errors
- **[CRITICAL #3 — `validate_intent` doesn't check for duplicate positions]**: No dedup check for existing positions on the same exchanges + symbol. If a BTCUSDT OKX↔HTX position is already open and a new intent arrives for the same pair — risk engine will allow it (if `max_open_positions` permits). Duplicate positions compound risk silently.
- **[CRITICAL #4 — Kill switch resets on restart]**: [`_sync_balance_on_startup()`](arbitrage/system/engine.py:122) runs once. If kill switch was triggered and bot restarts, the kill switch resets. No persistent kill switch state across restarts.

---

## 4. WebSocket & Market Data

### Strengths
- **Watchdog for WS** ([`ws_orderbooks.py:151`](arbitrage/system/ws_orderbooks.py:151)): Stale feed detection (>30s without updates) and automatic task restart — critical for production.
- **Reconnect with exponential backoff**: [`_run_ws_with_reconnect()`](arbitrage/system/ws_orderbooks.py:65) — correct approach, bounded by `_max_restart_attempts=5`.
- **Crossed book detection**: [`best_bid >= best_ask`](arbitrage/system/ws_orderbooks.py:91) — data validation prevents garbage signals.
- **Timestamp validation**: Rejecting future (>5s) and too-old (>60s) timestamps — correct handling of exchange timezone issues.

### Weaknesses
- **[`_get_ws_client()` always returns `None`](arbitrage/system/ws_orderbooks.py:268-275)**: Stub method creates a new WS instance instead of returning the running one from tasks. This means `health_status()` never correctly checks socket-level liveness.
- **New WS object created each reconnect**: [`_create_ws()`](arbitrage/system/ws_orderbooks.py:210) returns a new object every time. Old objects are never explicitly closed (`disconnect()` not called). **Socket leak**.
- **No initial full-orderbook snapshot**: WS connects and immediately receives incremental updates. If the first message is an update (not a full snapshot), the book state may be incomplete. Binance and OKX require initial snapshots before deltas.

### Critical Errors
- **[CRITICAL #5 — "Silent death" after reconnect loop exhaustion]**: If WS crashes and reconnect fails 5 times (max), the task is **permanently** removed. For 25 symbols × 4 exchanges = 100 tasks. If a network issue lasts >5 minutes (decay period) and all reconnects are exhausted, the bot stays without data for that symbol, and the watchdog won't restart it anymore. Need a "resurrection" mechanism after network recovery.
- **[CRITICAL #6 — Race condition in `_orderbooks` dict]**: [`_on_book`](arbitrage/system/ws_orderbooks.py:74-126) callback writes to `self._orderbooks` from multiple concurrent tasks without a lock. Python's GIL prevents data corruption but **not logical races** — `get_snapshot()` may read a partially updated state (e.g., bid updated but ask still old).

---

## 5. Strategies

### Strengths
- **Walk-the-book for realistic fill prices**: [`_check_price_spread()`](arbitrage/system/strategies/futures_cross_exchange.py:136-158) uses full orderbook depth, not best bid/ask. **Significantly more accurate** than top-of-book estimates.
- **Round-trip fee accounting**: [`total_fees_pct = entry_fees_pct * 2`](arbitrage/system/strategies/futures_cross_exchange.py:168) — correct logic accounting for both entry AND exit.
- **Net spread after fees**: [`net_spread_pct = spread_pct - total_fees_pct`](arbitrage/system/strategies/futures_cross_exchange.py:171) — trading on true margin, not gross.
- **Near-miss logging**: [`[SPREAD_NEAR_MISS]`](arbitrage/system/strategies/futures_cross_exchange.py:176-182) — invaluable for parameter calibration.

### Weaknesses
- **Statistical arbitrage (pairs trading) not in production**: File [`pairs_trading.py`](arbitrage/system/strategies/pairs_trading.py) exists but isn't enabled by default. Pairs trading is a powerful crypto strategy.
- **Static confidence scoring**: Confidence = `min(1.0, net_spread_pct / (min_spread_pct * 3))` — linear interpolation, no account of latency, volatility, or book depth.
- **No funding payment timing consideration**: Funding occurs every 8h. The funding arb strategy ([`_check_funding_rate()`](arbitrage/system/strategies/futures_cross_exchange.py:242)) doesn't account for *when* the next payment occurs. Entering 1 minute before funding yields a full payment; entering 7h after yields only 1/7.
- **Triangular arbitrage not truly implemented**: [`triangular_arbitrage.py`](arbitrage/system/strategies/triangular_arbitrage.py) exists but is not integrated into the engine.

### Critical Errors
- **[CRITICAL #7 — Funding arb uses top-of-book without walk-the-book]**: [`_check_funding_rate()`](arbitrage/system/strategies/futures_cross_exchange.py:242) uses `long_ob.ask` and `short_ob.bid` directly, without walk-the-book. For larger positions (>$5000), the spread cost will be significantly higher than estimated.
- **[CRITICAL #8 — No minimum order size check in strategy]**: Strategy emits intents without considering `min_order_size` of each exchange. The check only happens at execution level, which means useless intent generation for pairs that will always be rejected.

---

## 6. Exchange Adapters (REST + WS)

### Strengths
- **Session reuse with TCP connector pooling**: [`_get_session()`](arbitrage/exchanges/binance_rest.py:43) — correct approach, connection pool with limit=100.
- **Rate limiting per request**: `limiter.acquire()` before every HTTP call — correct integration.
- **429 handling with exponential backoff**: Both in REST clients and rate_limiter — double protection.
- **HTX gzip decompression**: [`_decompress()`](arbitrage/exchanges/htx_ws.py:94) — proper handling of compressed messages.
- **Binance recvWindow=10s**: Increased from 5s for Moscow→Hong Kong latency — practical tuning.

### Weaknesses
- **No idempotency keys for orders**: On retry of the same order (after timeout), the exchange may execute it twice. Binance supports `selfTradePrevention`, OKX supports `clientOrderId`, but they're not used.
- **Error handling too generic**: `except Exception as e: return {}` — swallow-all-errors pattern. [`binance_rest.py:100-101`](arbitrage/exchanges/binance_rest.py:100) returns empty dict for network error, timeout, JSON parse error — and the caller cannot distinguish one from another.
- **No rate limit monitoring/logging**: Rate limiter doesn't log when it throttles requests. In production, this matters — if the cycle gets throttled, latency increases silently.

### Critical Errors
- **[CRITICAL #9 — HTX signature timestamp on retry]**: [`_sign_request()`](arbitrage/exchanges/htx_rest.py:59-95) generates timestamp once per attempt (correctly recreated on retry). However, if `_sign_request` and actual HTTP send take >1 second, timestamp can become stale. HTX has a 5-minute window so this is rare but theoretically possible under heavy load.
- **[CRITICAL #10 — Binance `session.closed` check race]**: [`_get_session()`](arbitrage/exchanges/binance_rest.py:44) checks `self.session.closed` without a lock. Two concurrent calls may simultaneously create two session objects, the first of which becomes orphaned (memory/connection leak).

---

## 7. State Management

### Strengths
- **[`BotState`](arbitrage/core/state.py:76)**: Async-single-thread safe, logging on every position change.
- **Legacy compat aliases**: `okx_balance`, `htx_balance` — allows old code to work.
- **Per-strategy stats**: Separate statistics per strategy — correct for PnL attribution.

### Weaknesses
- **`Dict[tuple, PositionLike]` without type safety**: Keys are tuples with format depending on position type (`(strategy, symbol)` for ActivePosition, `(exchange, symbol, side)` for core Position). **This is error-prone** and makes refactoring difficult.
- **No state persistence**: On bot crash, all in-memory data (balances, positions, stats) are lost. Orphan position recovery on startup exists but doesn't replace true persistence.
- **[`calculate_pnl()`](arbitrage/core/state.py:244-263) only checks OKX and HTX**: If Binance or Bybit is used, PnL is not calculated.

### Critical Errors
- **[CRITICAL #11 — `remove_position()` silently ignores non-unique keys]**: If two strategies opened positions on the same symbol, `remove_position(strategy, symbol)` may remove the wrong one. In [`add_position()`](arbitrage/core/state.py:129), keys can collide — `(strategy, symbol)` could be identical for two different arbitrage trades on the same pair.

---

## 8. Configuration

### Strengths
- **Extensive env-driven configuration**: Nearly all parameters tunable without code changes.
- **Startup validation**: [`TradingSystemConfig.validate()`](arbitrage/system/config.py:225) — checks symbols, exchanges, equity.
- **Flexible symbol universe**: `TRADE_ALL_SYMBOLS=all` auto-discovers common pairs across exchanges.

### Weaknesses
- **No API key validation**: Credentials only checked for `not empty`, not validity (wrong key format, expired, wrong permissions).
- **Magic values in defaults**: `starting_equity=10_000`, but `max_open_positions=20` (after override). Unclear if these are calculated for specific conditions or just guesses.
- **No CLI config override**: Only `.env` supported. Inconvenient for A/B testing and different configs.

---

## 9. Testing

### Strengths
- **Mock exchanges available**: [`mock_exchanges.py`](arbitrage/test/mock_exchanges.py) — correct approach for testing without real API.
- **Multiple test files**: `test_all_audit_fixes.py`, `test_all_critical_fixes.py` etc. — shows effort to cover fixes.

### Weaknesses
- **No CI/CD**: No `.github/workflows/` or equivalent CI file. Tests must be run manually.
- **No property-based or stress tests**: Critical trading infrastructure should be stress-tested — race conditions, network failures, exchange downtime.
- **Confusing test file names**: 5 different test files with similar names — `test_all_fixes.py`, `test_all_critical_fixes.py`, `test_all_audit_fixes.py`, `test_all_review_fixes.py`, `test_critical_fixes.py`. Unclear what differs and which are current.

---

## 10. Additional Observations

### Strengths
- **Slippage model** ([`SlippageModel`](arbitrage/system/slippage.py)): Estimates based on notional, volatility, latency — realistic model.
- **Market Intelligence module**: RSI, MACD, Bollinger Bands, EMA, regime detection — full indicator suite.
- **Capital Allocator** ([`CapitalAllocator`](arbitrage/system/capital_allocator.py)): Allocates capital across strategies based on funding, volatility, trend.

### Weaknesses
- **Significant "dead code"**: `market_intelligence/regime.py`, `market_intelligence/order_flow.py`, `handlers/stock_*` — files exist but integration isn't visible.
- **`lowlatency/main.go`**: Go code in a Python project. If this is an external execution engine, it should be documented and integrated.
- **No health check endpoint**: No `/health` or equivalent for orchestration monitoring (Docker, k8s).
- **Inconsistent code style**: Part of the code uses Russian comments (in HTX clients), part uses English. This complicates maintenance in international teams.

---

## Summary Table

| Section | Strengths | Weaknesses | Critical Issues |
|---------|-----------|------------|-----------------|
| Architecture | Modularity, interfaces | 2 configs, monorepo | — |
| Execution | Two-phase, hedge guarantees | 3 execution layers | #1 orphan hedge, #2 race in verify |
| Risk | Multi-layered, circuit breaker | Two sets of defaults | #3 duplicate positions, #4 kill switch reset |
| WebSocket | Watchdog, reconnect, stale detection | Socket leak, no initial snapshot | #5 silent death, #6 race condition |
| Strategies | Walk-the-book, round-trip fees | No funding timing, static confidence | #7 top-of-book for funding arb, #8 min size |
| REST/WS Clients | Connection pooling, 429 handling | Generic exceptions, no idempotency | #9 timestamp race, #10 session race |
| State | Thread-safe, per-strategy stats | No persistence, tuple keys | #11 key collision |
| Config | Env-driven, validation | No CLI override | — |
| Testing | Mock exchanges | No CI/CD, confusing names | — |

---

## Trader's Verdict

The project is **engineering-sound** for a solo/small team. There's clear understanding of key arbitrage challenges (atomic execution, hedging, circuit breakers, fees). However, there are **6 critical errors** (#1-#6) and **5 serious issues** (#7-#11) that in production will lead to:

1. **Money loss** from orphan positions (unhedged legs)
2. **Silent data loss** (WS tasks dying permanently)
3. **Incorrect risk management** (duplicated positions, kill switch reset)

---

## Priority Fix Roadmap

| Priority | Fix | Critical Issue | Effort |
|----------|-----|----------------|--------|
| **P0** | Background orphan position monitor | #1, #5 | Medium |
| **P0** | Fix race conditions with asyncio.Lock | #2, #6, #10 | Medium |
| **P1** | Persistent kill switch state to disk | #4 | Low |
| **P1** | Position deduplication check | #3, #11 | Medium |
| **P1** | Walk-the-book for funding arb | #7 | Low |
| **P2** | Close expired WS clients properly | #5 | Low |
| **P2** | Initial full-orderbook snapshot on WS connect | #5 | Medium |
| **P2** | CI/CD pipeline, consistent code style | - | Medium |
| **P2** | Merge/consolidate dual config system | - | High |
| **P3** | Funding payment timing optimization | - | Medium |
| **P3** | Idempotency keys (clientOrderId) | - | Low |

---

## Per-Section Scores (out of 10)

| Section | Score | Notes |
|---------|-------|-------|
| Architecture | 7/10 | Solid modular design, held back by dual configs |
| Execution | 6/10 | Good two-phase design, but hedge failure handling is incomplete |
| Risk Management | 7/10 | Comprehensive checks, but missing dedup and persistence |
| WebSocket | 5/10 | Good watchdog/reconnect, but silent death is a showstopper |
| Strategies | 6/10 | Walk-the-book is excellent, funding arb needs depth pricing |
| Exchange Adapters | 7/10 | Solid REST/WS implementation, needs idempotency |
| State Management | 5/10 | No persistence and key collision risk are serious |
| Configuration | 6/10 | Env-driven is good, but no validation of API keys |
| Testing | 4/10 | Mock exchanges exist, but no CI/CD, confusing test files |
| **Overall** | **6/10** | Production-ready with critical fixes, not before |
