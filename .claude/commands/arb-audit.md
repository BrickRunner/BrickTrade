# Full Arbitrage System Audit & Launch Readiness

You are performing a **comprehensive audit** of the arbitrage trading system. Your goal is to determine if the bot is ready for real trading, identify everything that blocks profitability, fix Telegram UX issues, and fix critical bugs.

**This is NOT a read-only audit.** You must READ, ANALYZE, and FIX issues as you go.

---

## Phase 1: Code & Logic Errors

Scan every file in `arbitrage/`, `handlers/arbitrage_handlers_simple.py`, and related test files.

### What to look for and FIX:

**Runtime errors:**
- Imports that will fail (missing modules, circular imports)
- AttributeError risks (accessing attributes that may not exist)
- TypeError risks (wrong argument counts, wrong types)
- KeyError/IndexError risks (unvalidated dict/list access)
- Run `python -c "import arbitrage"` and `python -c "from arbitrage.system.engine import UnifiedArbitrageEngine"` to verify imports work

**Logic errors in trading flow:**
- Spread calculation correctness: verify formulas account for fees, slippage
- Entry/exit threshold logic: are comparisons correct (>=, <=, >, <)?
- Position tracking: can positions get out of sync between exchanges?
- PnL calculation: does it account for ALL costs (trading fees, funding rates, slippage)?
- Order size calculation: rounding, minimum lot sizes, contract sizes
- Race conditions in async code: shared state without locks

**Exchange API issues:**
- Incorrect API endpoints or parameters
- Missing/wrong authentication headers
- Response parsing that assumes fields always exist
- Rate limiting not handled
- WebSocket message format mismatches
- Reconnection logic gaps

**Fix all errors you find.** For each fix, note what was wrong and why it matters.

---

## Phase 2: Profitability Blockers

Analyze the system for issues that would prevent or reduce real profit.

### Check these specifically:

**1. Fee Model Accuracy**
- Read `arbitrage/system/fees.py` and verify fee rates match current exchange fee schedules
- Check: are fees applied to BOTH legs of each trade?
- Check: are fees applied on both entry AND exit?
- Check: maker vs taker fee distinction — does the bot use the correct fee type for its order type (IOC = taker)?
- Check: does spread threshold include fee compensation? (e.g., if round-trip fees = 0.12%, entry threshold must be > 0.12%)

**2. Slippage Model**
- Read `arbitrage/system/slippage.py` — is slippage estimation realistic?
- Does the bot use top-of-book price or walk the orderbook for the actual trade size?
- Is slippage factored into the minimum spread threshold?

**3. Spread Calculation**
- The spread formula must be: `spread = (price_sell_exchange - price_buy_exchange) / price_buy_exchange * 100`
- Both directions must be calculated
- Net spread after fees must be positive for a trade to be profitable
- Verify this in `arbitrage/system/strategies/` and `arbitrage/system/engine.py`

**4. Execution Quality**
- Read `arbitrage/system/execution.py`
- Time between detecting opportunity and placing orders — any unnecessary delays?
- Are both legs placed simultaneously (asyncio.gather) or sequentially?
- What happens if one leg fills and the other doesn't? Is there a hedge mechanism?
- Partial fill handling — does it leave orphaned positions?

**5. Funding Rate Integration**
- For perpetual futures: are funding rates tracked?
- If holding positions across funding intervals, is this cost/income accounted for?
- Can the bot exploit funding rate differentials?

**6. Capital Efficiency**
- Read `arbitrage/system/capital_allocator.py`
- Is capital split optimally between exchanges?
- Does position sizing account for margin requirements?
- Is there idle capital that could be utilized?

**7. Market Data Freshness**
- How old can orderbook data be before it's considered stale?
- Is there a staleness check before placing orders?
- What's the typical latency from exchange to decision?

**Fix profitability issues where possible.** For architectural changes needed, document them clearly.

---

## Phase 3: Telegram UX Improvement

Read ALL Telegram message formatting in:
- `handlers/arbitrage_handlers_simple.py`
- `arbitrage/core/notifications.py`
- Any other file that sends messages via `bot.send_message` or `message.answer`

### Requirements for Telegram messages:

**Status messages (when user checks bot status) must show:**
- Current mode (monitoring/dry-run/real) — clearly labeled
- Connected exchanges and their status (connected/disconnected)
- Active trading pairs
- Current spread for each pair (with direction arrows)
- Open positions (if any) with entry price, current PnL
- Account balances per exchange
- Uptime
- Use clean formatting with emojis for visual scanning

**Trade notifications must show:**
- Direction: which exchange buy, which sell
- Pair and size
- Entry spread % and net spread after fees
- Expected profit in USD
- Execution prices on both exchanges
- Fill status (full/partial)
- Timestamp

**Error/alert notifications must show:**
- Severity level (warning/critical)
- What happened (plain language, not stack traces)
- What action was taken (or needs to be taken)
- Timestamp

**Opportunity notifications (monitoring mode) must show:**
- Pair
- Spread % (gross and net after fees)
- Direction (buy X on exchange A, sell X on exchange B)
- Estimated profit for a given position size
- How long the opportunity has existed
- Whether it's actionable (spread > fees)

### Formatting rules:
- Use `HTML` parse mode (not Markdown — it breaks on special chars in trading)
- Use `<b>` for headers and key values
- Use `<code>` for numbers and prices
- Group related info visually (blank lines between sections)
- Keep messages concise but complete — no walls of text
- Use standard trading symbols: arrows for direction, colors via emoji for profit/loss
- Russian language for all user-facing messages (this is a Russian-language bot)
- All numbers formatted with proper decimal places (prices: exchange-appropriate, percentages: 2 decimal places, USD amounts: 2 decimal places)

**Apply all Telegram UX fixes directly.**

---

## Phase 4: Risk & Safety Validation

### Verify these safety mechanisms exist and work:

**1. Circuit Breaker**
- Read `arbitrage/system/circuit_breaker.py`
- Does it stop trading after N consecutive losses?
- Does it stop on max drawdown?
- Does it stop on unusual market conditions (extreme spread, low liquidity)?
- Can it be manually triggered (kill switch)?

**2. Position Limits**
- Maximum position size per pair
- Maximum total exposure across all pairs
- Maximum delta (net directional exposure)
- Are these enforced BEFORE order placement?

**3. Balance Protection**
- Minimum balance threshold (don't trade below X)
- Cross-exchange balance monitoring
- Alert on significant balance drop

**4. Order Validation**
- Orders validated before sending (price, size, side)
- Duplicate order prevention
- Maximum order size sanity check

**5. Graceful Degradation**
- What happens when one exchange WebSocket disconnects?
- What happens when REST API returns errors?
- What happens on network timeout during order placement?
- Are open positions safe during outages?

**Fix missing safety mechanisms.** These are critical for real trading.

---

## Phase 5: Launch Readiness Assessment

After completing phases 1-4, provide a final verdict.

### Output format:

```
══════════════════════════════════════════
       ARBITRAGE BOT AUDIT REPORT
══════════════════════════════════════════

📊 SUMMARY
- Files analyzed: X
- Bugs found & fixed: X
- Profitability issues found: X (fixed: Y)
- UX improvements made: X
- Safety gaps found: X (fixed: Y)

══════════════════════════════════════════

🔴 CRITICAL ISSUES (must fix before launch)
1. [Issue] — [File:Line] — [Status: FIXED / NEEDS MANUAL FIX]
   Description: ...
   Impact: ...

🟡 IMPORTANT ISSUES (should fix, can launch without)
1. [Issue] — [File:Line] — [Status: FIXED / NEEDS MANUAL FIX]
   Description: ...
   Impact: ...

🟢 MINOR ISSUES (nice to have)
1. ...

══════════════════════════════════════════

💰 PROFITABILITY ANALYSIS
- Minimum profitable spread: X% (after all fees)
- Current entry threshold: X%
- Fee breakdown per round-trip trade:
  - Exchange A maker/taker: X%
  - Exchange B maker/taker: X%
  - Total round-trip fees: X%
  - Estimated slippage: X%
  - Required spread for profit: X%
- Verdict: [PROFITABLE / MARGINAL / UNPROFITABLE at current settings]

══════════════════════════════════════════

🛡️ SAFETY ASSESSMENT
- Circuit breaker: [OK / MISSING / INCOMPLETE]
- Position limits: [OK / MISSING / INCOMPLETE]
- Balance protection: [OK / MISSING / INCOMPLETE]
- Hedge mechanism: [OK / MISSING / INCOMPLETE]
- Graceful shutdown: [OK / MISSING / INCOMPLETE]
- Kill switch: [OK / MISSING / INCOMPLETE]

══════════════════════════════════════════

🚀 LAUNCH READINESS: [READY / NOT READY / READY WITH CAVEATS]

Recommendation:
[Detailed recommendation on next steps — what to test, what mode to start in,
what to monitor, what thresholds to set]

══════════════════════════════════════════
```

---

## Execution Rules

- **DO** fix bugs and issues as you find them — don't just report
- **DO** verify fixes don't break other things
- **DO** run import checks after changes
- **DO** be brutally honest in the assessment — real money is at stake
- **DO NOT** change working trading logic without clear justification
- **DO NOT** add unnecessary complexity or over-engineer
- **DO NOT** modify `.env` files
- **DO NOT** change test files unless tests are testing fixed bugs
- **DO** use `asyncio.gather` patterns for parallel exchange operations
- **DO** preserve all existing safety checks — only add, never remove
- Work systematically file by file, phase by phase
- Use subagents for parallel analysis where appropriate
