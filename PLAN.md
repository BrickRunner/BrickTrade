# BrickTrade Refactor Plan: Institutional-Grade Arbitrage

## What Changes

Replace the current naive "price convergence" engine (`multi_pair_arbitrage.py` — 1889 lines) with a modular strategy-based architecture. The existing `arbitrage/strategies/` folder already has stubs for funding, basis, spot, futures, and triangular — but they only work with OKX+HTX and have no real execution. We rewrite everything to be 3-exchange (OKX/HTX/Bybit), production-grade.

## Architecture Overview

```
Market Data Engine (WebSocket + REST polling)
    ↓
Opportunity Detection (per-strategy)
    ↓
Strategy Router (picks best opportunity)
    ↓
Risk Management Engine (pre-trade checks)
    ↓
Execution Engine (atomic 2-leg orders)
    ↓
Position Manager (track, monitor, rebalance)
    ↓
Monitoring & Analytics (Telegram + metrics)
```

## File Plan

### DELETE (old naive logic)
- `arbitrage/core/arbitrage.py` — old single-pair engine (replaced)
- `arbitrage/core/multi_pair_arbitrage.py` — old multi-pair engine (replaced)
- `arbitrage/core/execution.py` — old execution manager (replaced)

### KEEP AS-IS (reuse)
- `arbitrage/exchanges/okx_rest.py` — OKX REST client
- `arbitrage/exchanges/htx_rest.py` — HTX REST client
- `arbitrage/exchanges/bybit_rest.py` — Bybit REST client
- `arbitrage/exchanges/okx_ws.py` — OKX WebSocket client
- `arbitrage/exchanges/htx_ws.py` — HTX WebSocket client
- `arbitrage/exchanges/__init__.py` — exchange exports
- `arbitrage/utils/logger.py` — logging
- `arbitrage/utils/helpers.py` — utility functions
- `arbitrage/core/notifications.py` — Telegram notifications (minor updates)
- `arbitrage/core/trade_history.py` — trade DB (keep)

### REWRITE (existing files, new content)
- `arbitrage/core/state.py` — new global state with per-strategy tracking
- `arbitrage/core/risk.py` — full risk engine
- `arbitrage/utils/config.py` — add strategy-specific config fields
- `arbitrage/strategies/base.py` — new BaseStrategy ABC + data models
- `arbitrage/strategies/funding_arb.py` — full funding rate arb (3 exchanges)
- `arbitrage/strategies/basis_arb.py` — full basis/cash-carry arb (3 exchanges)
- `arbitrage/strategies/strategy_manager.py` — new StrategyRouter
- `arbitrage/strategies/trade_executor.py` — new atomic execution engine

### CREATE NEW
- `arbitrage/strategies/stat_arb.py` — statistical arbitrage (z-score)
- `arbitrage/core/market_data.py` — unified market data engine
- `arbitrage/core/position_manager.py` — position tracking + rebalancing
- `arbitrage/core/metrics.py` — PnL, Sharpe, drawdown, latency tracking

### UPDATE
- `handlers/arbitrage_handlers_simple.py` — connect to new StrategyRouter
- `main.py` — update shutdown hooks

## Implementation Steps (in order)

### Step 1: Core Infrastructure
**Files**: `market_data.py`, `state.py`, `config.py`

- `MarketDataEngine`: unified class that fetches prices, funding rates, and spot prices from all 3 exchanges via REST polling (WS already exists for orderbooks)
  - `update_all()` — parallel fetch of tickers, funding, spot from OKX/HTX/Bybit
  - Stores: `futures_prices[exchange][symbol]`, `spot_prices[exchange][symbol]`, `funding_rates[exchange][symbol]`, `next_funding_times[exchange][symbol]`
  - Provides: `get_best_bid_ask(exchange, symbol)`, `get_funding_rate(exchange, symbol)`, `get_spot_price(exchange, symbol)`

- `BotState` rewrite:
  - Per-exchange balances (okx, htx, bybit)
  - Active positions dict: `{(strategy, symbol): ActivePosition}`
  - Trade history counters per strategy
  - Global PnL tracking

- `ArbitrageConfig` additions:
  - `funding_btc_threshold`, `funding_eth_threshold`, `funding_alt_threshold`
  - `basis_min_pct`, `basis_close_threshold`
  - `stat_arb_z_entry`, `stat_arb_z_exit`, `stat_arb_window`
  - `max_concurrent_positions`, `emergency_margin_ratio`

### Step 2: Strategy Base + Data Models
**Files**: `strategies/base.py`

- `BaseStrategy(ABC)`:
  - `name: str`
  - `async detect_opportunities(market_data) -> List[Opportunity]`
  - `async should_exit(position, market_data) -> (bool, reason)`
  - `get_required_data() -> Set[DataType]` (FUNDING, SPOT, FUTURES, etc.)

- `Opportunity` dataclass:
  - `strategy`, `symbol`, `long_exchange`, `short_exchange`
  - `expected_profit_pct`, `confidence`, `urgency`
  - `entry_params` (prices, sizes, etc.)

- `ActivePosition` dataclass:
  - `strategy`, `symbol`, `long_exchange`, `short_exchange`
  - `long_contracts`, `short_contracts`, `entry_time`
  - `entry_spread`, `accumulated_funding`, `total_fees`
  - `target_profit`, `stop_loss`

### Step 3: Three Strategies
**Files**: `funding_arb.py`, `basis_arb.py`, `stat_arb.py`

**A) Funding Rate Arbitrage** (`funding_arb.py`):
- Detect: for each symbol across all 3 exchanges, compute `funding_spread = max_rate - min_rate`
- Dynamic thresholds: BTC 0.02%, ETH 0.03%, ALT 0.05%
- Entry: LONG on lowest-funding exchange, SHORT on highest-funding exchange
- Exit conditions:
  1. `accumulated_funding_profit >= target_profit` (e.g., 0.1% of position)
  2. `funding_spread < exit_threshold` (spread collapsed)
  3. Risk engine triggers
- Profit tracking: accumulate funding payments every 8h, subtract fees

**B) Basis Arbitrage** (`basis_arb.py`):
- Detect: for each (exchange, symbol), compute `basis = (futures - spot) / spot * 100`
- Cross-exchange too: OKX spot + HTX futures, etc.
- Entry: `basis > fees + slippage + buffer` (~ > 0.3%)
- Cash & Carry: buy spot, sell futures
- Exit: basis < close_threshold OR risk trigger
- Note: perpetual futures have no expiry — basis profit comes from funding convergence

**C) Statistical Arbitrage** (`stat_arb.py`) — NEW:
- Maintain rolling spread history per exchange pair (e.g., 500 samples)
- Compute: `mean`, `std`, `z_score = (current_spread - mean) / std`
- Entry: `|z_score| > 2.5`
- Exit: `|z_score| < 0.5`
- Direction: if z > 2.5, spread is high → expect convergence → short the spread
- Uses `collections.deque` for O(1) rolling window

### Step 4: Strategy Router
**File**: `strategy_manager.py`

- `StrategyRouter`:
  - Holds all 3 strategy instances
  - Main loop: `update_data() → detect_all() → rank_opportunities() → execute_best()`
  - Ranking: by `expected_profit * confidence / urgency_decay`
  - Concurrency: max N simultaneous positions (configurable)
  - Conflict resolution: don't open opposing positions on same symbol

### Step 5: Execution Engine
**File**: `trade_executor.py`

- Atomic execution pipeline:
  1. **Pre-check**: balance, margin, circuit breakers
  2. **Liquidity check**: verify orderbook depth
  3. **Slippage estimation**: based on size vs book depth
  4. **Execute first leg** (risky exchange — HTX/Bybit): market/optimal_5
  5. **Verify first leg**: position check with retry
  6. **Execute second leg** (OKX IOC or market)
  7. **Verify second leg**
  8. **Position sync**: record in state
  9. **If any leg fails**: emergency hedge with 3 retries

- Close pipeline: same but reverse, with partial close support

### Step 6: Risk Management Engine
**File**: `risk.py`

- Pre-trade checks:
  - Exposure limit: total exposure < X% of balance
  - Per-exchange limit: single exchange < Y% of balance
  - Position count limit
  - Margin ratio > safe_level (per exchange)
  - Liquidation distance check

- Runtime monitoring:
  - Delta monitoring: `|long_total - short_total| < max_delta`
  - API health: track latency per exchange, circuit breaker if > threshold
  - Funding cost control: `expected_profit = funding_income - funding_cost - fees; if < 0 → force close`

- Emergency actions:
  - If margin_ratio < critical → close ALL positions
  - If API lag > threshold → pause trading
  - If price spike > threshold → pause + notify
  - Rebalancing: if `|long_qty - short_qty| > max_delta` → hedge immediately

### Step 7: Position Manager
**File**: `position_manager.py`

- Track all active positions across strategies
- Monitor each position's PnL in real-time
- Accumulate funding payments for funding_arb positions
- Detect stale positions (no price updates)
- Emergency close all

### Step 8: Metrics & Monitoring
**File**: `metrics.py`

- Per-strategy: trades, win_rate, PnL, avg_duration
- Global: total_pnl, max_drawdown, Sharpe ratio (rolling)
- Execution: avg_latency, slippage, fill_rate
- Funding: income, cost, net per 8h/24h

### Step 9: Handlers + Integration
**Files**: `handlers/arbitrage_handlers_simple.py`, `main.py`

- Connect handlers to new `StrategyRouter` instead of old engine
- Status shows per-strategy stats
- Scan shows opportunities from all 3 strategies
- Start/stop controls StrategyRouter
- Settings allow enable/disable individual strategies

## Key Design Decisions

1. **REST polling over WebSocket for multi-pair**: WS clients exist but are single-symbol. For 20+ pairs across 3 exchanges, REST batch tickers every 0.5-2s is more practical. WS can be added later for latency-critical paths.

2. **Sequential leg execution (not parallel)**: First leg = risky exchange, second leg = OKX IOC. This prevents one-sided fills — if first fails, we lose nothing. If second fails, we hedge first.

3. **3-exchange support throughout**: Every strategy iterates all exchange pairs (OKX↔HTX, OKX↔Bybit, HTX↔Bybit). This is 3 combinations per symbol.

4. **Funding arb as primary strategy**: Most reliable, market-neutral, predictable cash flows. Basis and stat arb are secondary.

5. **No spot trading in v1**: Cash & carry uses perpetual SHORT + perpetual LONG (across exchanges), not actual spot. Real spot buying would require different API integration. This is noted in the code.
