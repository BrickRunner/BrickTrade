# COMPREHENSIVE CODE AUDIT — Best Trader + Software Engineer Review

**Date:** 2026-04-07
**Scope:** Full trading bot codebase (~50+ files, ~30K+ LOC)

---

## 1. CORE ARBITRAGE (`arbitrage/core/`)

### 1.1 [`MarketDataEngine`](arbitrage/core/market_data.py)

**Strengths:**
- Clean abstraction over 4 exchanges (OKX, HTX, Bybit, Binance)
- Proper `asyncio.gather` with `return_exceptions=True` — one exchange failure won't kill the loop
- Latency tracking per exchange
- FIX #6: Spot instrument fetch errors are now properly surfaced (were silently swallowed)
- Instrument parsing correctly handles each exchange's unique symbol format
- Tick sizes and min order sizes are captured per-instrument

**Weaknesses:**
- `update_all()` calls four independent gather groups (futures, spot, funding, fees) — each makes sequential REST calls per exchange. Total cycle can be 2-4 seconds for all exchanges. This is too slow for real-time arbitrage.
- No data freshness validation — stale prices can be returned without warning
- `get_fees_bps()` returns a dict copy but fee rates are not actually populated in all code paths
- `common_pairs` is computed as simple intersection — doesn't filter by liquidity/availability

**Critical Errors:**
- **None critical, but** the `update_all` latency is a trading-quality risk: if a single exchange is slow (Bybit often >500ms on `/v5/market/tickers`), the entire update cycle is blocked. No per-exchange timeout is set on the gather call.

---

### 1.2 [`BotState`](arbitrage/core/state.py)

**Strengths:**
- FIX #1: Atomic JSON persistence (write-to-temp + `os.replace`) — positions survive crashes
- FIX #3: Backup file created before each save for corruption recovery
- FIX #15: Symbol lock tracking with timestamps enables proper cleanup of stale locks
- FIX #4: PnL calculation is no longer hardcoded to OKX/HTX
- FIX #14: `get_orderbooks()` no longer hardcoded to specific exchanges
- Supports both `ActivePosition` (strategy-level) and `Position` (legacy) types
- Reentrant per-symbol locks prevent double-entry

**Weaknesses:**
- Two parallel persistence systems: `arbitrage/core/state.py` saves to `data/arb_state.json`, while `arbitrage/system/state.py` saves to `data/open_positions.json`. These are NOT synchronized — a crash can leave them inconsistent.
- `get_positions_by_strategy()` returns `List[ActivePosition]` but internally iterates `self.positions.values()` which are dicts (serialized), not deserialized objects — `isinstance(p, ActivePosition)` will always fail after reload
- `has_position_on_symbol()` checks by string suffix matching — fragile and could match unrelated symbols (e.g., `BTCUSDT` matches `ETHBTCUSDT` if such a symbol existed)
- Legacy balance aliases (`okx_balance`, `htx_balance`, `bybit_balance`) are mutable but don't trigger `_save()`

**Critical Errors:**
1. **`get_positions_by_strategy()` returns empty list after reload** — positions are stored as dicts, deserialization only happens in `remove_position()`. Code that calls `get_positions_by_strategy()` expecting `ActivePosition` objects will get nothing after a restart.

---

### 1.3 [`MetricsTracker`](arbitrage/core/metrics.py)

**Strengths:**
- Clean per-strategy tracking
- Sharpe ratio with annualization
- Drawdown tracking (peak-to-trough)
- Cycle time monitoring

**Weaknesses:**
- **Sharpe ratio calculation is wrong** — assumes "~3 trades per day, 365 days" but this is a hardcoded constant, not data-driven. For a bot that trades infrequently, this gives wildly inflated or deflated Sharpe values. Should use actual time range: `(mean / std) * sqrt(trades_per_year * days_observed / 365)`.
- Only 1000 trade history limit (`maxlen=1000`) — reasonable for memory but means long-term stats are approximate
- No per-exchange metrics (win rate, avg slippage, avg latency per exchange)

**Critical Errors:**
- **None critical.** It's a simple tracker.

---

### 1.4 [`RiskManager`](arbitrage/core/risk.py)

**Strengths:**
- FIX #9: Net exposure check instead of gross — correctly recognizes hedged arbitrage positions
- FIX #8: Delta check uses actual per-leg sizes, not assumed 50/50
- FIX #14: Configurable minimum balance thresholds
- Circuit breaker on consecutive failures
- `_num()` helper safely coerces NaN and invalid types

**Weaknesses:**
- `can_open_position` uses `self._num(self.state.total_balance)` twice — redundant computation
- Emergency margin ratio (`emergency_margin_ratio`) is passed from config but never used in `can_open_position()` — only in `should_emergency_close()`
- `log_risk_status()` doesn't check for zero division or guard against state anomalies

**Critical Errors:**
- **None critical.** The risk logic is fundamentally sound.

---

### 1.5 [`NotificationManager`](arbitrage/core/notifications.py)

**Strengths:**
- FIX #12: Bot/user_id validated at construction — closes race window
- Clean separation of position open/close/emergency notifications

**Weaknesses:**
- No rate limiting on notifications — rapid-fire position opens/spreads could spam the user
- No retry on send failure

**Critical Errors:**
- **None.**

---

## 2. EXCHANGE ADAPTERS (`arbitrage/exchanges/`)

### 2.1 REST Clients (Binance, Bybit, HTX, OKX)

**Strengths:**
- All four share common patterns: lazy session creation, rate limiting, retry logic
- FIX CRITICAL #3: Lazy `_session_lock` creation avoids event-loop binding issues
- Proper TCP connector pooling (`limit=100`, `limit_per_host=30`, DNS cache)
- Each exchange has typed custom exception classes (Binance: `BinanceAPIError`, `BinanceNetworkError`, `BinanceTimeoutError`)
- HTX: FIX CRITICAL #10 — millisecond timestamps prevent replay attacks
- OKX: FIX CRITICAL #6 — `time.time()` instead of deprecated `datetime.utcnow()`
- Rate limiter integration via `get_rate_limiter()` with 429 backoff handling
- Binance `RECV_WINDOW = 5000ms` (correct max)

**Weaknesses:**
- **No request signing for public endpoints** is correct, but `_get_session()` is called even for public requests — wastes the lock acquisition
- Bybit: `RECV_WINDOW` is a string `"5000"`, not int. Works because API accepts string, but inconsistent.
- HTX uses `import time as _htx_time` — confusing alias. Standard `import time` works.
- OKX testnet URL is the same as live (`www.okx.com`) — OKX doesn't have a separate testnet, but this comment is misleading
- No per-request timeout customization — all public requests use `total=5` timeout, but some endpoints (e.g., instrument lists) can take longer
- Error handling in `_public_request` returns `{"code": "429", "msg": "rate_limited"}` — but callers might not check for this specific format

**Critical Errors:**
- **None critical in the REST clients themselves.** They are well-structured.

---

### 2.2 WebSocket Clients (Binance, Bybit, HTX, OKX)

**Strengths:**
- FIX CRITICAL C: Replaced `async for message in ws` with explicit `asyncio.wait_for(ws.recv(), timeout=30)` — this fixes the infamous "silent death" bug where `async for` pattern silently exits on connection drop without raising `ConnectionClosed`
- Callback-isolated exception handling — a bad callback doesn't kill the recv loop
- Clean reconnect logic with exponential-ish backoff
- Ping/pong keepalive configured (`ping_interval=20, ping_timeout=10`)

**Weaknesses:**
- **OKX WS**: No subscription confirmation check — the code sends subscribe message on line 70 but doesn't verify subscription response before assuming data will arrive. Binance has the same issue.
- **No rate limiting on reconnects** — if an exchange ban/403s the WS, the `while self.running` loop will hammer reconnect indefinitely
- **No last heartbeat tracking in public WS clients** — private WS has `_last_msg_ts`, but public WS (orderbook) doesn't track it, so stale connections can't be detected
- **OKX symbol formatting**: The logic `f"{base}-USDT-SWAP"` assumes all symbols end with `USDT`. If a symbol like `USDTUSDT` is passed, it would become `USDT-USDT-SWAP` which is incorrect.

**Critical Errors:**
- **SILENT WS DEATH** (the file in open tabs confirms this is a known issue): Despite the FIX CRITICAL C, the `.env` file note says "the WS message loop dies silently". This means on the production environment, the explicit recv loop may still fail. Possible causes:
  1. If `ws.recv()` raises `ConnectionClosedError` instead of `ConnectionClosed`, it wouldn't be caught by the inner loop's `except websockets.exceptions.ConnectionClosed` (different exception subclass in some websockets versions)
  2. The `while self.running` outer loop doesn't have a heartbeat check — if `ws` object becomes a zombie without closing, the inner `ws.recv()` might never return and never timeout

---

### 2.3 [`PrivateWsManager`](arbitrage/exchanges/private_ws.py)

**Strengths:**
- FIX CRITICAL H: Auth failure cap at 3 attempts — prevents log spam + rate limit abuse
- FIX #2: Robust message loop with `reconnect_reason` tracking
- Proper channel subscription confirmations
- `_last_msg_ts` tracking with `time.monotonic()` for zombie detection
- Handles OKX, HTX, Bybit in a single file

**Weaknesses:**
- **HTX WS authentication** uses `datetime.now(timezone.utc).strftime()` without milliseconds — REST client uses `time.time()` with ms for security. The WS auth could be vulnerable to replay if two requests fire in the same second
- **Bybit private WS** implementation is incomplete — the file cuts at ~line 300+ and the Bybit handler is less battle-tested than OKX
- **No balance aggregation** across sub-accounts (OKX supports multiple sub-accounts)

**Critical Errors:**
- **None critical**, but the HTX WS auth timestamp precision is a potential issue.

---

## 3. SYSTEM MODULES (`arbitrage/system/`)

### 3.1 [`TradingSystemEngine`](arbitrage/system/engine.py)

**Strengths:**
- FIX CRITICAL #1/#8: Module-level cached env var constants — hot loop doesn't call `os.getenv()` every cycle
- FIX #3: Orphaned order cleanup on startup
- FIX #8: Orphaned position scanning on startup — prevents "Insufficient margin" from ghost contracts
- FIX #5: Log restored positions on startup
- FIX #1: Circuit breaker checks before intent execution
- FIX CRITICAL #2: Symbol-level blacklisting instead of global kill switch for single-pair failures
- `_filter_underfunded_exchanges()` prevents costly "first leg fills → second leg rejects → hedge back" scenarios
- MARGIN_REJECT_COOLDOWN: Blocks problematic exchanges for 30 min instead of killing all trading
- Phantom position detection after 3 close failures — handles crash/test data ghosts
- Loss streak tracking with per-symbol cooldown
- Detailed structured logging (`[ALLOCATION]`, `[STRATEGY_SELECT]`, `[INTENTS]`, etc.)

**Weaknesses:**
- `_get_min_notional()` and `_filter_underfunded_exchanges()` are cut off in the file (beyond line 650) — need to verify their correctness
- `estimated_slippage_bps` uses `average_book_depth_usd=2_000_000` hardcoded — this should come from actual orderbook data
- The cycle runs on a fixed interval (`cycle_interval_seconds`) — in a quiet market this is wasteful polling; in a volatile market it may be too slow
- `run_forever()` has bare `except Exception: continue` — the engine will never stop even on systemic failures, only log errors
- Balance sync only happens once on first cycle (`_balance_synced` flag) — if an external withdrawal/deposit happens, the engine won't detect it

**Critical Errors:**
- **None critical**, but the engine's resilience to systemic failures means it could continue trading in a degraded state indefinitely without operator awareness.

---

### 3.2 [`AtomicExecutionEngine`](arbitrage/system/execution.py)

**Strengths:**
- FIX CRITICAL #3: Lazy lock creation for event-loop safety
- FIX CRITICAL #1: `dict.setdefault` for atomic per-symbol lock creation
- ABBA deadlock prevention — locks acquired in alphabetical order
- Reliability ranking for leg ordering — most reliable exchange goes first
- Centralized constants via env vars instead of scattered magic numbers
- Hedge fill threshold configurable

**Weaknesses:**
- **Typo**: `_acquire_exchange_lockes` and `_release_exchange_lockes` (should be "locks") — minor but looks unprofessional
- `_determine_exit_leg_order` sorts by reliability but doesn't account for current exchange health — if OKX is circuit-broken but still rated #0, it would still be tried first
- `HEDGE_FILL_THRESHOLD = 0.98` (env var) — if hedge fills at 98% of intended size, the residual 2% is not tracked or re-hedged
- No slippage alerting — if realized slippage exceeds threshold, the code triggers kill switch (fair) but doesn't log the actual slippage amount for post-mortem

**Critical Errors:**
- **None critical**, but the 2% residual unhedged position from partial fills is a real risk.

---

### 3.3 [`AtomicExecutionEngineV2`](arbitrage/system/execution_v2.py)

**Strengths:**
- Two-phase execution design: preflight → entry → verification → hedge
- Per-exchange margin requirements instead of hardcoded 0.15
- Clear enum-based state machine (`ExecutionPhase`, `ExecutionStatus`)
- Renamed from original to avoid name collision

**Weaknesses:**
- Coexists with `execution.py`'s `AtomicExecutionEngine` — confusion risk
- Uses `logging.getLogger(__name__)` instead of project logger pattern

**Critical Errors:**
- **None critical.**

---

### 3.4 [`WsOrderbookCache`](arbitrage/system/ws_orderbooks.py)

**Strengths:**
- FIX CRITICAL #6: Async lock protects concurrent reads/writes
- FIX CRITICAL #5: Active WS instances tracked for liveness checks
- Watchdog task for auto-restart of dead WS connections
- Restart count decay after stability period
- Per-symbol invalid update counting
- Max depth entries cap to prevent unbounded memory growth

**Weaknesses:**
- `_stale_after_sec = 3.0` but watchdog only runs every 10 seconds — a stale orderbook between 3-10 seconds won't be detected
- WS instances in `_ws_instances` are never cleaned up if `_run_ws_with_reconnect` returns normally (e.g., unsupported exchange) — memory leak potential
- `_restart_counts` and `_restart_last_ts` dicts grow unbounded — old keys are never removed

**Critical Errors:**
- **Stale orderbook window** (3-10 sec blind spot between staleness threshold and watchdog interval) could lead to executing on outdated prices.

---

### 3.5 [`FuturesCrossExchangeStrategy`](arbitrage/system/strategies/futures_cross_exchange.py)

**Strengths:**
- Walk-the-book (`SlippageModel.walk_book`) for realistic fill prices
- FIX #2: Correct round-trip fee accounting (4 legs total: entry + exit on both sides)
- Dual-direction spread checking — only keeps the better direction
- Confidence scales with spread excess over threshold
- Spread near-miss logging for diagnostics
- Per-direction cooldown prevents rapid re-entries
- Market-neutral design: no PnL-based SL, only spread convergence TP and timeout

**Weaknesses:**
- `est_notional = balance * 0.05` — assumes 5% of balance per leg, but the actual allocation comes from the engine. This could walk-the-book with the wrong depth
- Funding rate arbitrage intent has a potential conflict with price spread intent — both could fire on the same exchange pair in the same cycle
- `_check_depth()` call (line 205) isn't visible in the file — need to verify it checks both leg depths correctly
- `max_holding_seconds = 3600` (1 hour) seems aggressive for a spread arb that may need more time to converge

**Critical Errors:**
- **None critical.** The strategy logic is sound.

---

### 3.6 [`CashAndCarryStrategy`](arbitrage/system/strategies/cash_and_carry.py)

**Strengths:**
- Single-exchange delta-neutral design (no cross-exchange risk)
- Correct fee rates per exchange (VIP-0 spot and perp)
- Annualized funding rate threshold
- Basis spread check
- 5-min signal cooldown (appropriate for slow-changing funding rates)

**Weaknesses:**
- `_MIN_FUNDING_APR_PCT = 5.0` hardcoded at module level but constructor parameter defaults to same — two sources of truth
- Doesn't check spot wallet availability (spot buy requires spot balance, not futures balance)
- No max holding cost check — if funding turns negative, the position bleeds until exit

**Critical Errors:**
- **None critical.**

---

### 3.7 Config and Models

**`config.py` (system):**
- **Strengths:** Frozen dataclasses (immutable after creation), env var fallback, comprehensive risk/execution/strategy config
- **Weaknesses:** `_as_bool` checks `if value is None` but `os.getenv()` already returns `None`, so the wrapper is redundant
- **Critical:** None

**`models.py`:**
- **Strengths:** Clean dataclass design, frozen where appropriate
- **Weaknesses:** `ExecutionReport` defined here duplicates the one in `execution_v2.py`
- **Critical:** None.

---

## 4. LIVE ADAPTERS (`arbitrage/system/live_adapters.py`)

**Strengths:**
- Proper bridging between core `MarketDataEngine` and `MarketDataProvider` interface
- WS integration with configurable refresh intervals per data type
- FIX CRITICAL #6: WS cache access is async with lock-protected reads
- `_safe_float` helper for robust coercion

**Weaknesses:**
- **Mixes responsibilities:** The `LiveMarketDataProvider` does market data fetching, balance tracking, fee tracking, AND technical indicators (RSI, EMA, Bollinger, MACD). This violates SRP.
- **Multiple refresh intervals** (futures 1s, spot 5s, funding 60s, depth 2s, balance 5s, fee 900s) create complex timing — on a single `get_snapshot()` call, different data points have different ages
- **`_last_refresh_ts` vs `_last_futures_ts` etc.:** There are 6 independent timestamps but only one `get_snapshot()` entry point — a snapshot could mix 1-sec-old futures prices with 5-sec-old spot prices

**Critical Errors:**
- **Data age mixing**: A snapshot can contain data points of wildly different freshness. The engine checks max orderbook age, but doesn't check funding rate age. A 60-second-old funding rate could be stale.

---

## 5. MARKET INTELLIGENCE (`market_intelligence/`)

### 5.1 Architecture

**Strengths:**
- Comprehensive: indicators, regime detection, opportunity scoring, portfolio analysis
- Feature engineering with z-score normalization
- Regime model with multiple interaction factors (ADX, EMA cross, BB, RSI, vol)
- Opportunity scorer with adaptive weighting and signal decay
- Data validation pipeline
- Structured JSON logging
- Order flow analysis (optional)
- Robust correlation instead of simple Pearson

**Weaknesses:**
- **Extreme complexity**: The regime model has ~20 configurable coefficients. Without proper calibration/backtesting, this is curve fitting.
- **No backtesting integration visible** — the `market_intelligence` module generates features and scores, but there's no visible way to validate that these features actually predict profitable trades
- **Missing files referenced in code**: `regime.py`, `scorer.py`, `validation.py`, `structured_log.py`, `statistics.py` are imported but not listed in the file tree — these may exist, but if not, the module won't load
- **Order book depth not included** in collected features — missing a key indicator of liquidity risk

### 5.2 `MarketDataCollector`

**Strengths:**
- Exchange-specific circuit breaker for data collection
- Rate limiter integration
- Queue-based data storage with configurable maxlen
- Funding rate normalization to 8h equivalent

**Weaknesses:**
- `maxlen=720` (default) = 720 data points. At 1-second intervals this is 12 minutes of data. For regime detection, this may not be enough history.
- Circuit breaker backoff: `30 * 2^(failures-1)` up to 300s — reasonable but could be more adaptive
- REST polling for data collection adds latency to the trading cycle

---

## 6. TELEGRAM BOT INTEGRATION (`main.py`, `handlers/`)

**Strengths:**
- Clean separation of concerns: handlers organized by feature
- `_EngineState` encapsulation — engine state is properly isolated
- Graceful shutdown with position closing
- Multiple handler modules avoid single file bloat
- Command routing is explicit and maintainable

**Weaknesses:**
- `main.py` does **too much**: registers ALL handlers from ALL sub-modules, starts the scheduler, runs the Telegram bot, manages health check server. It's a god function.
- `arbitrage_handlers_simple.py` imports from `arbitrage.system.*` directly — creates a dependency chain from Telegram bot to trading engine
- Error handling in handler callbacks: `try: self.task.exception() except Exception: pass` — silently swallows exceptions from the task

**Critical Errors:**
- **None critical**, but the architecture mixes concerns (Telegram UI + trading engine).

---

## 7. UTILITIES (`arbitrage/utils/`)

### 7.1 `config.py`
- **Strengths:** Dataclass-based config with env var loading via dotenv
- **Weaknesses:** `load_dotenv()` called at import time — makes testing harder. Sensitive defaults (e.g., `monitoring_only = False`) could lead to accidental live trading if `.env` is missing.

### 7.2 `logger.py`
- **Strengths:** Hourly rotating file handler with date/hour directory structure
- **Weaknesses:** Uses `datetime.now()` (local time) instead of UTC — log rotation can behave unexpectedly during DST transitions

### 7.3 `rate_limiter.py` (referenced but not read)
- Referenced across the codebase but the file content isn't visible in the file listing

---

## 8. CRITICAL ISSUES SUMMARY

### Critical (Money at Risk)

| # | Issue | File(s) | Impact |
|---|-------|---------|--------|
| C1 | **Two parallel persistence systems** not synchronized | `core/state.py` + `system/state.py` | After crash, positions may be inconsistent or duplicated |
| C2 | **WS silent death** in production | `.env` note + WS clients | Connection dies without raising `ConnectionClosed` → stale prices → bad trades |
| C3 | **`get_positions_by_strategy()` returns empty after reload** | `core/state.py:212-216` | Strategies can't see restored positions → double entry possible |
| C4 | **Data age mixing in snapshots** | `live_adapters.py` | Funding rate up to 60s old, mixing with 1s-orderbooks → inaccurate decisions |
| C5 | **Partial hedge residual (2%) not tracked** | `execution.py` | Small unhedged position accumulates over multiple trades |
| C6 | **`load_dotenv()` at import time with dangerous defaults** | `utils/config.py` | If `.env` missing, `monitoring_only=False` + `dry_run=False` could trigger live trading |

### High (Degraded Quality, No Immediate Money Loss)

| # | Issue | File(s) | Impact |
|---|-------|---------|--------|
| H1 | **Sharpe ratio formula** wrong | `core/metrics.py:56-70` | Misleading performance metrics |
| H2 | **`has_position_on_symbol()` substring matching** | `core/state.py:224-233` | False positive symbol matches |
| H3 | **WS no subscription confirmation** in public clients | `okx_ws.py`, `binance_ws.py` | Subscription failure undetected |
| H4 | **Balance sync only once** on startup | `engine.py` | External deposits/withdrawals not detected |
| H5 | **Module complexity of Market Intelligence** | `market_intelligence/` | Overfitted regime model without backtesting validation |
| H6 | **Typo in method names** `_acquire_exchange_lockes` | `execution.py:62` | Minor, but confusing for maintenance |

### Medium (Performance/Debugging)

| # | Issue | File(s) | Impact |
|---|-------|---------|--------|
| M1 | **REST polling cycle latency** 2-4 seconds | `market_data.py:update_all()` | Missed fast arbitrage opportunities |
| M2 | **`run_forever()` bare except** | `engine.py:120-128` | Engine never stops on systemic failure |
| M3 | **`max_holding_seconds=3600` aggressive** | `futures_cross_exchange.py:248` | Legitimate spread positions closed prematurely |
| M4 | **Log rotation uses local time** | `logger.py:30` | DST transitions cause log gaps or duplicates |
| M5 | **Stale orderbook blind spot 3-10 sec** | `ws_orderbooks.py` | Trades on outdated prices possible |
| M6 | **Restart counts dict unbounded** | `ws_orderbooks.py:41` | Memory leak over long-running sessions |

---

## 9. OVERALL ASSESSMENT

### Trading Architecture: **B+ (Good)**

The arbitrage strategy is fundamentally sound:
- Delta-neutral with proper hedging
- Round-trip fee accounting
- Walk-the-book for realistic fills
- No directional PnL-based stops (correct for spread arb)
- Multiple strategies available (futures cross-exchange, cash & carry)
- Good risk management with circuit breakers, cooldowns, and loss streak tracking

**But** the position sizing is very small (`position_size: 0.01` BTC ≈ $1000 at 100K BTC), meaning:
- Per-trade profit at 0.1% spread ≈ $1.00
- After 4-leg fees (~0.10-0.15%), net profit is $0-$0.50 per trade
- This is below the minimum order sizes on most exchanges
- The code has safeguards (`min_notional_override`) to handle this, but it's an edge case

### Software Engineering: **B (Good with notable issues)**

**Positives:**
- Excellent use of dataclasses and frozen configs
- Strong async/await patterns
- Lazy initialization patterns done right
- Proper exception isolation
- Good structured logging
- Test files exist (many `test_*.py` files)
- Multiple prior audit rounds with FIX comments

**Negatives:**
- Two parallel state systems that can diverge
- God-function in `main.py`
- Missing SRP in `LiveMarketDataProvider`
- Some FIX comments reference issue numbers that aren't documented outside code
- Typo in public method names
- Import-time `load_dotenv()` in config

### Recommendation Priority:

1. **Fix C6**: Add `monitoring_only = True` and `dry_run_mode = True` as fail-safe defaults
2. **Fix C1**: Consolidate the two persistence systems into one
3. **Fix C2**: Add explicit WS heartbeat validation with forced reconnect
4. **Fix C3**: Deserialize positions in `get_positions_by_strategy()`
5. **Fix H1**: Correct the Sharpe ratio formula
6. **Fix M1**: Parallelize the `update_all()` cycle with per-exchange timeouts

---

*End of audit.*
