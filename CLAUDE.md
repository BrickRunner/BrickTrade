# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a dual-purpose Telegram bot written in Python using aiogram 3.0:

1. **Exchange Rate Bot**: Tracks currency exchange rates from the Central Bank of Russia with notifications, thresholds, and statistics
2. **Arbitrage Bot**: Professional cross-exchange perpetual futures arbitrage trading bot for OKX ↔️ Bybit

## Development Commands

### Setup and Installation

```bash
# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Activate (Linux/Mac)
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Setup environment variables
cp .env.example .env
# Then edit .env with your credentials
```

### Running the Bot

```bash
# Run main Telegram bot (includes both features)
python main.py

# Run arbitrage bot standalone
python -m arbitrage.main
```

### Testing

```bash
# Test arbitrage with debug logging (monitoring mode)
python test_arbitrage_debug.py

# Test multi-pair monitoring (no API keys required)
python test_multi_pair.py

# Test OKX API (requires OKX keys if not in mock mode)
python test_okx_api.py

# Test Bybit public API (NO personal keys required)
python test_bybit_public_api.py

# Test both exchanges (mode-dependent)
python test_both_exchanges.py

# Quick functionality test
python quick_test.py

# Demo tracking
python demo_tracking.py
```

**Note**: Most tests work in monitoring mode without personal API keys. Only advanced trading mode tests require API credentials.

## High-Level Architecture

### Main Bot Structure (Exchange Rate)

- **Entry Point**: `main.py` - Initializes bot, dispatcher, registers handlers, manages lifecycle
- **Configuration**: `config.py` - Loads environment variables, defines constants, currency symbols
- **Database**: `database.py` - SQLite async operations with SQL injection protection via whitelisting
- **API Layer**: `api.py` - Fetches exchange rates from Central Bank of Russia (CBR) APIs
- **Scheduler**: `scheduler.py` - Asynchronous notification scheduler with duplicate prevention
- **Handlers**: `handlers/` directory - Separate modules for different bot features (basic, settings, thresholds, stats, arbitrage)

### Arbitrage Bot Structure

The arbitrage bot is a complex trading system with the following architecture:

#### Core Components (`arbitrage/core/`)

- **BotState** (`state.py`): Central state manager tracking:
  - WebSocket connection status for both exchanges
  - Orderbook data (bids/asks) with thread-safe updates
  - Open positions across exchanges
  - Balance tracking (OKX, Bybit, total)
  - Trade statistics and PnL

- **ArbitrageEngine** (`arbitrage.py`): Main trading logic:
  - Continuously monitors orderbook spreads
  - Calculates two directional spreads: `spread1 = (bybit_bid - okx_ask) / okx_ask * 100` and `spread2 = (okx_bid - bybit_ask) / bybit_ask * 100`
  - Entry logic: Opens positions when spread >= entry_threshold
  - Exit logic: Closes positions when spread <= exit_threshold
  - Integrates with RiskManager and ExecutionManager

- **RiskManager** (`risk.py`): Risk controls:
  - Position size validation based on balance
  - Delta monitoring (ensures `abs(okx_pos + bybit_pos)` stays within limits)
  - Max risk per trade enforcement
  - Balance checks before trading

- **ExecutionManager** (`execution.py`): Order execution:
  - Simultaneous order placement on both exchanges
  - Limit IOC (Immediate-Or-Cancel) orders with configurable timeout
  - Partial fill handling with immediate hedging
  - Position closing logic

- **MultiPairArbitrageEngine** (`multi_pair_arbitrage.py`): Multi-pair monitoring:
  - Monitors multiple trading pairs simultaneously
  - Tracks spreads and opportunities across pairs
  - Notifies users of best arbitrage opportunities
  - Manages opportunity lifetimes to avoid spam

- **NotificationManager** (`notifications.py`): Telegram integration for arbitrage alerts

#### Exchange Integration (`arbitrage/exchanges/`)

- **WebSocket Clients** (`okx_ws.py`, `bybit_ws.py`):
  - Real-time orderbook streaming
  - Auto-reconnect on disconnection
  - Heartbeat/ping-pong management
  - Callback-based orderbook updates

- **REST Clients** (`okx_rest.py`, `bybit_rest.py`):
  - Order placement and cancellation
  - Position and balance queries
  - Exchange-specific authentication (HMAC signatures)
  - HTTP session management with connection pooling

#### Utilities (`arbitrage/utils/`)

- **ArbitrageConfig** (`config.py`): Configuration management:
  - Loads settings from environment variables
  - Validates configuration parameters
  - Provides separate configs for OKX and Bybit
  - **Supports 3 modes**:
    - MOCK mode (full simulation, no real APIs)
    - DRY_RUN mode (real data, no order execution)
    - REAL mode (full trading with real money)

- **Logger** (`logger.py`): Structured logging with separate log files for trades, errors, and general activity

#### Testing (`arbitrage/test/`)

- **Mock Exchanges** (`mock_exchanges.py`): Safe testing without real API calls or money

## Important Architectural Patterns

### Arbitrage Operating Modes

The arbitrage bot supports multiple operating modes for different use cases:

1. **MONITORING_ONLY Mode** (Default, Recommended):
   - `ARB_MONITORING_ONLY=true`
   - OKX: Uses authenticated API with READ-ONLY keys from .env
   - Bybit: Uses **public API only** - no personal keys required
   - NO trading functionality - only monitors spreads and sends notifications
   - Safest mode for beginners and analysis

2. **MOCK Mode** (Development):
   - `ARB_MONITORING_ONLY=false`, `ARB_MOCK_MODE=true`
   - Uses mock clients from `arbitrage/test/mock_exchanges.py`
   - Simulated data for development and testing

3. **DRY_RUN Mode** (Advanced Testing):
   - `ARB_MONITORING_ONLY=false`, `ARB_MOCK_MODE=false`, `ARB_DRY_RUN_MODE=true`
   - Uses real WebSocket data and APIs
   - Doesn't place actual orders
   - Requires personal API keys

4. **REAL Mode** (DANGEROUS):
   - All flags set to `false`
   - Full real trading with real money
   - NOT recommended unless you know what you're doing

Mode selection is implemented in `arbitrage/utils/config.py:validate()` and `handlers/arbitrage_handlers.py`.

### State Management

Both bots use centralized state management:
- **Exchange Rate Bot**: Database-backed user settings with async SQLite
- **Arbitrage Bot**: In-memory `BotState` object shared across all components via dependency injection

### Handler Registration Pattern

Handlers in `main.py` are registered using lambda predicates:
```python
dp.message.register(handler_func, lambda m: m.text == "Button Text")
dp.callback_query.register(handler_func, lambda c: c.data.startswith("prefix:"))
```

When adding new handlers, follow this pattern and register them in the `register_handlers()` function.

### Async-First Design

All I/O operations are asynchronous:
- Database queries use `aiosqlite`
- HTTP requests use `aiohttp`
- WebSocket connections are async
- Scheduler runs in background with `asyncio.create_task()`

### Security Patterns

1. **SQL Injection Prevention**: `database.py` uses `ALLOWED_SETTINGS_FIELDS` whitelist before dynamic SQL
2. **API Key Management**: All secrets loaded from `.env`, never hardcoded
3. **Input Validation**: All user inputs sanitized (length limits, type checks)
4. **Graceful Shutdown**: Signal handlers ensure proper cleanup of connections

### Arbitrage Trading Flow

1. **Initialization**: Create bot → Initialize components → Connect WebSockets
2. **Monitoring**: Continuous orderbook updates via WebSocket callbacks
3. **Opportunity Detection**: Engine calculates spreads every cycle
4. **Risk Check**: RiskManager validates trade feasibility
5. **Execution**: ExecutionManager places simultaneous orders on both exchanges
6. **Hedging**: If one leg fills and other doesn't, immediate market hedge
7. **Exit Monitoring**: Continuously check if exit threshold reached
8. **Position Close**: Close both positions simultaneously

### Integration Points

The arbitrage bot integrates with the main Telegram bot via:
- `handlers/arbitrage_handlers.py`: Telegram command handlers
- Global singleton instances for bot lifecycle (`_arb_bot`, `_arb_task`, etc.)
- `NotificationManager` receives aiogram `Bot` instance for sending alerts

## Configuration

### Required Environment Variables

```bash
# Telegram Bot (Always Required)
BOT_TOKEN=your_telegram_bot_token

# Arbitrage Mode (Recommended: monitoring only)
ARB_MONITORING_ONLY=true          # Default: monitoring only, no trading
ARB_MOCK_MODE=true                # For development (ignored if monitoring_only=true)
ARB_DRY_RUN_MODE=true             # For testing (ignored if monitoring_only=true)

# Exchange API Keys
# OKX: REQUIRED even in monitoring mode (read-only keys)
# Bybit: NOT required in monitoring mode (uses public API only)
OKX_API_KEY=...                   # REQUIRED - read-only keys
OKX_SECRET=...                    # REQUIRED
OKX_PASSPHRASE=...                # REQUIRED
BYBIT_API_KEY=...                 # NOT needed in monitoring mode
BYBIT_SECRET=...                  # NOT needed in monitoring mode

# Monitoring Parameters
MIN_SPREAD=1.0                    # Minimum spread to report (%) - default 1.0%
UPDATE_INTERVAL=1                 # Update frequency (seconds)
MIN_OPPORTUNITY_LIFETIME=5        # Min seconds before notification - filters false signals
```

See `.env.example` for complete list with detailed comments.

### Default Settings (Exchange Rate Bot)

- Timezone: UTC+3 (Moscow)
- Currencies: USD, EUR
- Notification time: 08:00
- Notification days: Monday-Friday (1-5)

## Database Schema

### Exchange Rate Bot

**user_settings**:
- `user_id` (PRIMARY KEY): Telegram user ID
- `currencies`: Comma-separated currency codes
- `notify_time`: HH:MM format
- `days`: Comma-separated day numbers (1-7)
- `timezone`: UTC offset integer
- `last_sent_date`: ISO date string for duplicate prevention

**thresholds**:
- `id` (AUTO INCREMENT): Threshold ID
- `user_id` (FOREIGN KEY): References user_settings
- `currency`: Currency code
- `value`: Threshold value (float)
- `comment`: Optional user comment
- Indexes on `user_id` and `currency` for performance

## Critical Implementation Notes

### Arbitrage Bot Safety

The arbitrage bot is designed for **monitoring only by default**. Key safety measures:

- **Default Mode**: `ARB_MONITORING_ONLY=true` - Only monitors spreads, no trading
- **OKX**: Requires READ-ONLY API keys from .env (for authenticated market data)
- **Bybit**: Uses public API only - NO personal keys required
- **ALWAYS check `config.monitoring_only` first** before any trading operation
- **Order Placement Prevention**: `ExecutionManager._place_order()` respects `dry_run_mode` and returns mock results
- **Configuration Validation**: `config.validate()` requires OKX keys but NOT Bybit keys in monitoring mode
- **Handler Simplification**: Trading bot handlers (start/stop/status/stats) removed - only monitoring remains
- When modifying execution logic, ensure fail-safe hedging still works
- WebSocket reconnection must preserve state integrity

**For developers enabling trading modes:**
- NEVER bypass `config.monitoring_only`, `config.mock_mode`, or `config.dry_run_mode` checks
- NEVER bypass RiskManager checks
- Test thoroughly in MOCK mode before considering DRY_RUN
- REAL mode should only be used with extreme caution and proper risk management

### Scheduler Duplicate Prevention

The scheduler in `scheduler.py` uses TWO mechanisms to prevent duplicates:
1. In-memory set `sent_this_minute` keyed by `(user_id, hour, minute)`
2. Database field `last_sent_date` for cross-restart protection

When modifying scheduler, maintain both checks.

### FSM States

The bot uses aiogram FSM states defined in `states.py`:
- `DateForm.waiting_for_date`: Waiting for date input
- `InlineThresholdForm.entering_value`: Setting threshold value
- `InlineThresholdForm.entering_comment_manual`: Adding threshold comment

### Logging Levels

- Exchange Rate Bot: Logs to `bot.log` and stdout
- Arbitrage Bot: Logs to `logs/arbitrage_YYYYMMDD.log`, `logs/trades_YYYYMMDD.log`, `logs/errors_YYYYMMDD.log`

Configure via `ARB_LOG_LEVEL` environment variable.

## File Locations

### Core Bot Files
- Entry: `main.py`
- Handlers: `handlers/*.py`
- Database: `database.py`
- Configuration: `config.py`

### Arbitrage Bot Files
- Main: `arbitrage/main.py`
- Core logic: `arbitrage/core/*.py`
- Exchanges: `arbitrage/exchanges/*.py`
- Config: `arbitrage/utils/config.py`

### Tests
- Root level: `test_*.py` files
- Mock implementations: `arbitrage/test/mock_exchanges.py`

### Logs
- Main bot: `bot.log`
- Arbitrage: `logs/` directory

### Database
- SQLite file: `rates.db` (configurable via `DB_PATH`)
