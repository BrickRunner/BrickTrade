# Comprehensive Code Audit — BrickTrade Arbitrage System

> **Auditor Perspective:** Senior quantitative trader + principal systems engineer.
> **Date:** 2026-04-07
> **Scope:** All production Python code in `arbitrage/`, `market_intelligence/`, `handlers/`, and entry points.

---

## 1. ARCHITECTURE & SYSTEM DESIGN

### Strengths

- **Clean separation of concerns.** The split between `arbitrage/core/` (market data, risk, state, notifications) and `arbitrage/system/` (execution, strategies, risk engine, circuit breakers) shows architectural maturity. Each component has a single responsibility.
- **Strategy Runner pattern.** The `StrategyRunner` in [`strategy_runner.py`](arbitrage/system/strategy_runner.py:17) treats strategies as pure functions of `MarketSnapshot -> List[TradeIntent]`, making them easy to test, compose, and extend.
- **Risk-Engine separation.** The `RiskEngine` in [`arbitrage/system/risk.py`](arbitrage/system/risk.py:19) validates every intent with explicit checks (kill switch, latency, leverage, slippage, stale orderbook, drawdown, inventory balance, position deduplication) before any execution can occur.
- **Market Intelligence Pipeline.** The `market_intelligence/` module is production-grade: regime detection, feature engineering, portfolio risk analysis, data health validation, and structured logging. Few retail arb bots go this deep.
- **Circuit breaker pattern.** The `ExchangeCircuitBreaker` in [`circuit_breaker.py`](arbitrage/system/circuit_breaker.py:18) with exponential backoff and auto-recovery is textbook correct and well-implemented.

### Weaknesses

- **Two parallel execution systems.** Both `execution.py` (AtomicExecutionEngine) and `execution_v2.py` (AtomicExecutionEngineV2) exist, plus `execution_v2.py` is imported but not clearly wired into the main engine. This creates confusion about which execution path is actually used in production.
- **Two parallel risk engines.** `arbitrage/core/risk.py` (RiskManager) and `arbitrage/system/risk.py` (RiskEngine) serve overlapping purposes. They check similar constraints but with different thresholds and logic. Divergence between them means a position could pass one check but fail the other.
- **Configuration sprawl.** Config is scattered across env vars (`os.getenv()`), dataclass defaults, and multiple files (`config.py`, `arbitrage/utils/config.py`, `arbitrage/system/config.py`). A single `ArbitrageConfig` or `TradingSystemConfig` at the top level would be much cleaner.

### Critical Issues

- **CRITICAL: No explicit shutdown/cleanup path.** The WS connections, HTTP sessions, and asyncio tasks are not guaranteed to be cleaned up on `SIGTERM`/`SIGINT`. This has been identified in the `.env the WS message loop dies silently` note — WebSocket loops die silently and orphan sessions remain open.
- **CRITICAL: Hot-loop `os.getenv()` calls.** In [`engine.py`](arbitrage/system/engine.py:228), `os.getenv()` is called in the trading cycle for position exit parameters. While FIX #9 partially addressed this, `EXIT_TAKE_PROFIT_USD`, `EXIT_MAX_HOLD_SECONDS`, etc. are still read every cycle in [`_process_open_positions()`](arbitrage/system/engine.py:389).

---

## 2. MARKET DATA LAYER

### Strengths

- **Unified abstraction.** The `MarketDataEngine` in [`market_data.py`](arbitrage/core/market_data.py:34) provides a single interface for prices, funding rates, spot prices, contract sizes, tick sizes, and fee rates across all exchanges.
- **Walk-the-book slippage estimation.** The `SlippageModel.walk_book()` in [`slippage.py`](arbitrage/system/slippage.py:33) correctly computes volume-weighted average fill prices from orderbook depth, which is essential for real-world arb profitability estimates.
- **Per-exchange fee tracking.** Fee rates are fetched live and cached, with fallback to well-documented defaults. The comments in [`futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py:32) cite explicit sources and VIP-0 rates.
- **WebSocket orderbook cache.** The `WsOrderbookCache` in [`ws_orderbooks.py`](arbitrage/system/ws_orderbooks.py:23) with watchdog, reconnection, and lock-protected reads is robust infrastructure.

### Weaknesses

- **`update_all()` error swallowing.** In [`market_data.py`](arbitrage/core/market_data.py:128) the `update_all()` method uses `return_exceptions=True` and only logs errors. There is no aggregate health signal — the engine could be working with stale data for dozens of cycles without any circuit tripping.
- **No orderbook timestamp validation in hot path.** While the risk engine checks `max_orderbook_age_sec`, the market data layer itself has no staleness guard. Old data can be fetched and used if the exchange API returns cached responses.
- **Contract size mapping is fragile.** In [`market_data.py`](arbitrage/core/market_data.py:212-295), each exchange has hardcoded parsing logic for contract sizes, tick sizes, etc. If any exchange changes its API response format, the system silently uses wrong sizes (defaulting to 1.0), leading to order sizes being off by orders of magnitude.

### Critical Issues

- **CRITICAL: Race condition in `MarketDataEngine._latency` dict.** Concurrent `update_*` calls write to shared dicts (`futures_prices`, `spot_prices`, etc.) without locks. While CPython's GIL typically protects dict writes, this is not guaranteed and can corrupt state under PyPy or future Python versions.
- **CRITICAL: WebSocket message loop silent death.** The `.env the WS message loop dies silently` file confirms this is a known issue. The `_run_ws_with_reconnect` loop in [`ws_orderbooks.py`](arbitrage/system/ws_orderbooks.py:84) can exit without logging when `_on_book` callback raises an unhandled exception.

---

## 3. STRATEGIES

### Strengths

- **`FuturesCrossExchangeStrategy` fee math is correct.** The round-trip fee calculation in [`futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py:177) accounts for all four legs (entry long + entry short + exit long + exit short), which many amateur arb bots miss.
- **Walk-the-book for realistic pricing.** Both `_check_price_spread()` and `_check_funding_rate()` use walked orderbook prices instead of top-of-book, which is the only correct approach for size > $500.
- **`CashAndCarryStrategy` APR math is fixed.** The comment at [`cash_and_carry.py`](arbitrage/system/strategies/cash_and_carry.py:147) explicitly documents the old broken formula and the corrected one.
- **Cooldown per direction.** The per-pair cooldown (`_last_signal_ts`) prevents rapid re-entries on the same signal, which is good.
- **Multiple strategies cleanly isolated.** Cross-exchange arb, cash-and-carry, pairs trading, triangular arb, and funding harvesting are well-separated.

### Weaknesses

- **Funding arb timing approximation is broken.** In [`futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py:332), `time_since_last_funding = 0.0` is hardcoded. This means the `adjusted_income` calculation always assumes the worst case (0 remaining fraction), making the funding arb much more selective than intended.
- **Strategy returns `TradeIntent` but engine overrides notional.** The strategy computes its own edge_bps and stop_loss_bps, but the engine in [`engine.py`](arbitrage/system/engine.py:230) overrides `proposed_notional` based on allocator limits. The strategy has no awareness of how much will actually be traded, making its confidence scores meaningless at execution time.
- **No position sizing signal from strategies.** Strategies signal "go/no-go" but the `DynamicPositionSizer` in [`position_sizer.py`](arbitrage/system/position_sizer.py:35) runs separately. The sizing logic is disconnected from the signal generation logic.
- **Spread arbitrage confidence formula is naive.** `confidence = min(1.0, net_spread_pct / (min_spread_pct * 3.0))` is linear and has no empirical calibration. A 1.5x threshold spread gets confidence 0.5, which is arbitrary.

### Critical Issues

- **CRITICAL: Funding rate arb uses `time_since_last_funding = 0.0`.** This hardcoded zero in [`futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py:332) means `remaining_fraction = 1.0`, which then multiplies `fr_diff_pct * 1.0` — but the comment says "if only 1h remains, adjust income" which suggests the intention was the opposite. This is a logic inversion that makes funding arb signals unreliable.
- **CRITICAL: Duplicate intent generation.** The `on_market_snapshot` method checks both AB and BA directions for price spread arb, then ALSO checks funding rate arb separately. If both a spread opportunity and a funding opportunity exist on the same pair, both intents fire simultaneously, causing double-allocation on the same symbol-pair.

---

## 4. EXECUTION ENGINE

### Strengths

- **Pre-flight margin checks.** The execution engine in [`execution.py`](arbitrage/system/execution.py:92) verifies both exchanges have margin before placing any orders. This prevents the "leg-1 fills, leg-2 rejected" scenario.
- **Leg ordering with reliability rank.** The engine determines which leg to execute first based on exchange reliability ranking, minimizing the risk of partial fills on unreliable exchanges.
- **Orphan position detection on startup.** The engine scans for untracked positions on all exchanges at startup and logs warnings.
- **Lock ordering prevents deadlock.** Per-symbol and per-exchange locks are acquired in alphabetical order, preventing ABBA deadlocks.
- **Hedge retry loop.** Second leg gets up to 3 attempts with increasing aggressiveness (IOC first, then market orders).

### Weaknesses

- **No partial fill accounting.** If leg-1 partially fills (e.g., 70% of size), the second leg still uses original `notional_usd`, not the adjusted `first_effective` size. Line 228 does attempt this with `second_notional = min(notional_usd, first_effective)`, but `first_effective` comes from the result dict which may not be populated for all exchanges.
- **Maker mode disabled by default.** `use_maker_taker: bool = False` in execution config means fee savings from post-only orders are never attempted. Maker fees are typically 60-80% lower.
- **`execute_multi_leg_spot()` called without validation.** When `intent.metadata.get("legs")` is truthy, the engine calls `execute_multi_leg_spot()` which is not shown to be implemented for the live venue.
- **Hardcoded depth estimate.** `est_book_depth_usd=2_000_000` is hardcoded in the engine's execution call, ignoring actual orderbook depth.

### Critical Issues

- **CRITICAL: Kill switch triggers cascade failures.** In [`engine.py`](arbitrage/system/engine.py:349), a single symbol's hedge failure triggers `trigger_kill_switch(permanent=False)`, which pauses ALL trading across ALL symbols. This is too aggressive — the fix attempts to set a symbol-specific blacklist first, but then also triggers a global kill switch.
- **CRITICAL: No order acknowledgment verification.** After placing an order, the code checks `first_result.get("success")` but does not verify the order actually exists on the exchange via a separate API call. Some exchanges return `"success"` even when the order is rejected or partially filled.
- **CRITICAL: `place_maker_taker` hybrid is referenced but unimplemented.** The code in [`execution.py`](arbitrage/system/execution.py:161) checks `hasattr(self, '_place_maker_leg')` but `_place_maker_leg` is never defined in the `AtomicExecutionEngine` class, making this code path dead code.

---

## 5. RISK MANAGEMENT

### Strengths

- **Multiple layers of protection.** Pre-trade checks (balance, exposure, position count, circuit breaker), runtime checks (delta, API latency), and post-trade checks (slippage realization, kill switch) form a good defense-in-depth.
- **Drawdown-aware risk reduction.** The `PortfolioAnalyzer` in [`portfolio.py`](market_intelligence/portfolio.py:94) penalizes risk during portfolio drawdowns with a proportional multiplier.
- **Inventory imbalance checks.** Only the two exchanges involved in a trade are checked for balance imbalance, not all exchanges globally, which avoids false rejections.

### Weaknesses

- **Two risk engines with different thresholds.** `arbitrage/core/risk.py` (`RiskManager`) and `arbitrage/system/risk.py` (`RiskEngine`) use different defaults for `max_position_pct`, `max_delta_pct`, etc. A position could pass one and fail the other.
- **No position-level PnL tracking in risk.** The risk engine does not track unrealized PnL of open positions against stop-loss levels. It only has time-based and spread-based exits.
- **Exposure check uses `size_usd` which is notional, not margin.** For leveraged products, this overstates actual risk. A $1000 position at 10x leverage is $100 at risk, not $1000.

### Critical Issues

- **CRITICAL: Kill switch cooldown resets on midnight but no persistence.** The kill switch state is stored in memory. If the bot restarts, kill switch state is lost and trading resumes immediately with no cooldown.
- **CRITICAL: No slippage circuit breaker.** Realized slippage is checked AFTER execution, but there is no pre-execution check that historical realized slippage per exchange is within bounds.

---

## 6. MARKET INTELLIGENCE MODULE

### Strengths

- **Regime detection is sophisticated.** The `RegimeModel` in [`regime.py`](arbitrage/system/regime.py) uses multiple indicators (EMA cross, ADX, RSI, Bollinger Bands, ATR, liquidation clustering) to classify market regimes with confidence smoothing and stability guards.
- **Feature engineering is comprehensive.** The `FeatureEngine` in [`feature_engine.py`](arbitrage/system/feature_engine.py) computes 30+ features including RSI, MACD, ADX, ATR, BB width, funding z-score, spread dynamics, volume trends, and liquidation cascade risk.
- **OHLCV vs tick-data fallback.** When OHLCV candles are unavailable, the system gracefully falls back to bid/ask proxies with a degradation flag.
- **Adaptive ML weight optimization.** The `OnlineWeightOptimizer` in the scorer pipeline can learn from real trading outcomes to dynamically adjust feature weights.
- **Drawdown-aware allocation.** The portfolio analyzer penalizes risk allocation during drawdowns, which is a professional-grade feature.

### Weaknesses

- **No trade-level feedback loop.** The ML weights are loaded from a file but the system doesn't log trade outcomes (PnL, entry/exit reasons) in a format that `OnlineWeightOptimizer` can consume. Without labeled data, the weights are static.
- **Regime classification uses fixed thresholds.** Despite the sophisticated math, the logits-to-regime mapping still uses hardcoded thresholds (RSI 68 for overheated, 32 for panic, etc.) that are not adaptive to changing market conditions.
- **Collector runs REST polling at high frequency.** With 720 data points and multiple exchanges, the collector can make hundreds of REST calls per cycle, hitting rate limits during high-demand periods.
- **Correlation computation is O(n²).** Pairwise correlations to BTC and between all symbols is done every cycle without caching.

### Critical Issues

- **CRITICAL: `collector.py` has its own `ExchangeCircuitBreaker` class** that shadows `arbitrage/system/circuit_breaker.py`. It uses different backoff parameters (300s max vs 600s) and different failure tracking. If both are active, the same exchange could be counted differently by different parts of the system.
- **CRITICAL: `feature_engine.py` reads `candles` dict but the collector's candle fetching in `collector.py` has no error handling** — if one exchange fails to return candles, the entire feature computation for that symbol falls back to degraded tick-data proxies silently.

---

## 7. EXCHANGE ADAPTERS

### Strengths

- **Consistent interface across all exchanges.** All four REST clients (Binance, OKX, Bybit, HTX) implement the same method signatures: `get_instruments`, `get_tickers`, `get_funding_rates`, `get_balance`, `place_order`, `cancel_order`, `get_position`.
- **Rate limiting is integrated.** Every REST call goes through `get_rate_limiter()`, which uses a token bucket with per-exchange rates and 429 handling.
- **Session management with lock protection.** All clients use `asyncio.Lock()`-protected session creation, fixing the race condition from earlier versions.
- **WebSocket private channels for real-time updates.** The `private_ws.py` module uses WebSocket push for balances, orders, and positions instead of REST polling.
- **Retry logic with exponential backoff.** All clients retry failed requests 3 times with increasing delays.
- **Binance recvWindow increased to 10s.** The comment in [`binance_rest.py`](arbitrage/exchanges/binance_rest.py:40) notes the 10s window was increased from 5s to handle Moscow→HK latency — good operational awareness.

### Weaknesses

- **Error responses are inconsistent.** OKX returns `{"code": "0"}` on success, Binance returns raw lists, Bybit returns `{"retCode": 0}`, HTX returns `{"status": "ok"}`. The market data layer handles these but adds parsing error risk.
- **Timeout values are hard-coded.** Different endpoints use 5s, 10s, 15s timeouts with no centralized configuration. Market data endpoints should have shorter timeouts than trading endpoints.
- **No idempotency for order placement.** If a network failure occurs after an order is placed but before the response is received, the retry will place the same order again, potentially doubling up.
- **Binance spot and futures share one `session` object** but use different base URLs. If the futures connection times out, the spot connection might be affected.

### Critical Issues

- **CRITICAL: OKX client uses `datetime.utcnow()` for signing**, which is deprecated and can drift if the system clock is not perfectly synced. The HMAC signature depends on timestamp accuracy. Should use `time.time()` with milliseconds instead.
- **CRITICAL: HTX signing uses `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")`** with second precision. If two requests are made within the same second, they get the same timestamp and identical signatures, which HTX may reject as replay attacks.
- **CRITICAL: None of the WebSocket clients have heartbeat timeout.** The `websockets.connect()` calls set `ping_interval=20, ping_timeout=10` but do NOT handle `websockets.ConnectionClosed` exceptions in the message loop. When the connection drops, the loop exits silently.

---

## 8. STATE MANAGEMENT & PERSISTENCE

### Strengths

- **Atomic writes with temp file + `os.replace`.** The state persistence in [`state.py`](arbitrage/core/state.py:124) uses write-to-temp then atomic rename, preventing corruption on crash.
- **Position locking with per-symbol guards.** `try_lock_symbol()` prevents race conditions when multiple strategies try to open on the same symbol simultaneously.
- **Position removal by string key.** The updated removal logic uses consistent string keys instead of the old tuple-based approach.

### Weaknesses

- **Legacy position aliases create confusion.** `okx_balance`, `htx_balance`, `bybit_balance` as instance attributes alongside the `balances` dict is redundant and a source of potential inconsistency.
- **`get_positions_by_strategy` returns raw dict values, not Position objects.** The method body is missing — it appears to not deserialize the values properly.
- **No versioning in persisted state.** If the `ActivePosition` dataclass schema changes, old persisted state will fail to deserialize.

### Critical Issues

- **CRITICAL: `_load()` silently ignores parse errors.** If `arb_state.json` is corrupted (e.g., from a crash during write), the entire state is wiped and restored from empty defaults. Any open positions tracked in memory would be forgotten, losing the ability to manage or close them.
- **CRITICAL: `_lock_holders` tracking is incomplete.** The `_lock_holders` dict is initialized but there is no visible cleanup logic for stale locks, which could cause permanent deadlocks across restarts.

---

## 9. LOW-LATENCY EXECUTION VENUE

### Strengths

- **HTTP-based Go sidecar.** The `LowLatencyExecutionVenue` in [`lowlatency.py`](arbitrage/system/lowlatency.py:14) delegates to a Go binary at `127.0.0.1:8089`, which provides sub-millisecond order routing for latency-sensitive strategies.
- **Fallback handling.** Unsupported operations (OCO, RFQ) return graceful `False` responses instead of crashing.

### Weaknesses

- **No session locking.** The `_session` field has no lock protection, unlike the REST clients.
- **Hardcoded localhost URL.** The URL is configurable via `LOWLATENCY_URL` env var but defaults to `http://127.0.0.1:8089` with no TLS. If the Go binary isn't running, all order placement fails with connection refused.
- **No health check.** There is no ping endpoint to verify the sidecar is alive before sending orders.

---

## 10. POSITION MONITOR

### Strengths

- **Background orphan detection.** The `PositionMonitor` in [`position_monitor.py`](arbitrage/system/position_monitor.py:20) runs independently and can hedge or close positions that the main engine lost track of.
- **Configurable check interval.** Default 30 seconds is reasonable for a safety net.

### Weaknesses

- **30-second check interval is too slow.** In fast markets, an orphan position can accrue significant losses in 30 seconds.
- **No emergency close mechanism.** Orphans are hedged, not closed. If the exchange API is unresponsive, the monitor just logs an error and waits for the next cycle.

---

## 11. FEE OPTIMIZATION

### Strengths

- **`FeeOptimizer` tracks maker/taker stats per exchange.** Fill rates and fee savings are computed and can inform runtime decisions.
- **`FeeTierTracker` maps VIP levels.** The tier structure in [`fee_tier_tracker.py`](arbitrage/system/fee_tier_tracker.py:42) with volume-based progression is realistic and useful for tracking upgrade progress.

### Weaknesses

- **Maker/taker hybrid execution disabled.** `use_maker_taker: bool = False` in config means the optimizer never runs.
- **Fee tier tracker is not automatically updated.** It requires manual `update_tier()` calls with volume data. The exchange APIs can fetch this automatically but it's not wired up.

---

## 12. RATE LIMITING

### Strengths

- **Per-exchange token bucket.** The `ExchangeRateLimiter` in [`rate_limiter.py`](arbitrage/utils/rate_limiter.py:66) correctly implements token bucket with refill, burst capacity, and 429 backoff.
- **Per-exchange rates.** Defaults are set to conservative values based on documented API limits.

### Weaknesses

- **`acquire()` sleeps inside the lock.** In [`rate_limiter.py`](arbitrage/utils/rate_limiter.py:97), `await asyncio.sleep(wait)` is called while holding `bucket.lock`, blocking all other tasks from checking the bucket during the sleep.
- **No warm-up period.** The rate limiter starts with a full bucket. If the bot starts 10 exchanges polling simultaneously, all requests fire at once.

---

## 13. TELEGRAM BOT / HANDLERS

### Strengths

- **Comprehensive UI.** The bot covers settings, thresholds, arbitrage controls, short-bot, statistics, and market intelligence reports — all accessible from Telegram.
- **Startup market report.** The `/start` command sends an initial market intelligence report if scoring is enabled.

### Weaknesses

- **Currency bot and arb bot are coupled.** The bot handles both CBR currency rates and trading — these are unrelated concerns that should be separate bots.
- **No rate limiting on Telegram commands.** Users can spam `/start` or trigger scans every second, potentially overwhelming the API.

---

## SUMMARY: Critical Issues Priority List

| # | Severity | Issue | File | Impact |
|---|----------|-------|------|--------|
| 1 | 🔴 CRITICAL | WebSocket message loops die silently | `ws_orderbooks.py:84`, `private_ws.py` | Stale orderbooks → trades on wrong prices |
| 2 | 🔴 CRITICAL | Kill switch cascade on single symbol failure | `engine.py:349` | All symbols lose trading for a single pair's failure |
| 3 | 🔴 CRITICAL | State file corruption wipes all positions | `state.py:_load()` | Orphaned positions with no tracking |
| 4 | 🔴 CRITICAL | `time_since_last_funding = 0.0` hardcoded | `futures_cross_exchange.py:332` | Funding arb signals are unreliable |
| 5 | 🔴 CRITICAL | No WebSocket heartbeat exception handling | All `*_ws.py` clients | Silent disconnection, trades on stale data |
| 6 | 🔴 CRITICAL | OKX signing uses deprecated `datetime.utcnow()` | `okx_rest.py:64` | Timestamp drift → auth failures |
| 7 | 🔴 CRITICAL | Duplicate risk engine with conflicting thresholds | `core/risk.py` vs `system/risk.py` | Inconsistent risk enforcement |
| 8 | 🟡 HIGH | `os.getenv()` in hot loop | `engine.py:389-396` | Prevents runtime changes without restart |
| 9 | 🟡 HIGH | Maker/taker execution path is dead code | `execution.py:161` | Missing `_place_maker_leg()` method |
| 10 | 🟡 HIGH | HTX signing timestamp has second precision | `htx_rest.py:74` | Replay attack vulnerability |
| 11 | 🟡 HIGH | Two parallel execution systems | `execution.py` vs `execution_v2.py` | Unclear which is used in production |
| 12 | 🟡 HIGH | `acquire()` sleeps while holding rate limiter lock | `rate_limiter.py:97` | Unnecessary serialization of requests |

---

## OVERALL ASSESSMENT

**Trading Quality: B+**

The strategy logic, risk management, and market intelligence pipeline show professional-grade design. The regime detection, adaptive scoring, fee tracking, and circuit breakers are well-conceived. However, the funding arb math bug and the kill-switch cascade could cause real trading losses.

**Code Quality: B**

The codebase is well-structured with clean abstractions, but suffers from: (1) two parallel systems for execution and risk, (2) configuration sprawl, (3) silent failures in the WebSocket layer, and (4) insufficient persistence/versioning of state. The test coverage is substantial (8 test files with ~150K lines of tests), but many tests are testing fixes rather than full integration scenarios.

**Production Readiness: B-**

The system could run productively in small-size mode ($50-200/trade) but should not be scaled up until: (a) the 6 critical issues above are resolved, (b) the dual execution/risk systems are unified, (c) WebSocket reliability is hardened, and (d) position state corruption is handled gracefully.
