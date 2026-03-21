# Arbitrage Code Cleanup & Deficiency Audit

You are performing a **deep code cleanup and deficiency analysis** of the arbitrage trading system.

Your goals:
1. Find and **delete** all dead/unused code
2. Find and **report** system deficiencies

---

## Phase 1: Dead Code Detection & Removal

Systematically scan the entire `arbitrage/` directory and any arbitrage-related files outside it.

### What to look for:

**Unused imports:**
- Scan every `.py` file in `arbitrage/` for imports that are never referenced in the file body
- Remove them

**Unused functions/methods:**
- Find functions and methods that are defined but never called from anywhere in the project
- Search globally (including `main.py`, `handlers/`, `test_*.py`) before deciding something is unused
- If a function is only used in test files that are themselves dead, the function is dead too
- Remove them

**Unused variables and constants:**
- Find module-level constants, class attributes, or variables that are assigned but never read
- Remove them

**Dead classes:**
- Find classes that are defined but never instantiated or subclassed anywhere
- Remove them

**Unreachable code:**
- Code after unconditional `return`, `break`, `continue`, `raise`
- Branches that can never be true (e.g., `if False:`, contradictory conditions)
- Remove them

**Stale commented-out code:**
- Large blocks of commented-out code (not explanatory comments)
- Remove them (git history preserves everything)

**Orphaned files:**
- `.py` files in `arbitrage/` that are never imported or referenced
- Report them (ask user before deleting entire files)

**Deprecated/legacy patterns:**
- Old exchange integrations that were replaced (e.g., old Bybit REST if replaced by new one)
- Leftover mock/test code in production modules
- Remove or flag them

### Rules for removal:

- **DO NOT** remove code that is used dynamically (via `getattr`, string-based dispatch, config-driven loading)
- **DO NOT** remove `__init__.py` exports that serve as public API
- **DO NOT** remove handler registrations even if they look unused (they're registered via lambdas)
- **DO** search for all references before removing anything
- **DO** verify imports across the entire project, not just within `arbitrage/`
- When removing a function, also remove its tests if they only test that function

### Process:

1. Use `Grep` and `Glob` to find all Python files in `arbitrage/` and arbitrage-related handlers
2. For each file, identify all defined names (functions, classes, constants)
3. Search globally for each name to determine if it's used
4. Collect dead code into a list
5. Remove dead code file by file
6. After cleanup, verify no import errors by checking remaining imports are valid

---

## Phase 2: System Deficiency Analysis

After cleanup, analyze the remaining code for architectural and functional deficiencies.

### Categories to evaluate:

**1. Error Handling Gaps**
- Missing try/except around network calls (REST, WebSocket)
- Unhandled edge cases (empty orderbook, zero price, NaN values)
- Silent error swallowing (bare `except: pass`)
- Missing retry logic for transient failures

**2. Race Conditions & Concurrency**
- Shared state accessed from multiple coroutines without locks
- TOCTOU (time-of-check-time-of-use) issues in balance/position checks
- WebSocket callbacks modifying state while engine reads it

**3. Data Integrity**
- Orderbook staleness not checked (using old data for decisions)
- Missing validation of exchange API responses
- Floating point precision issues in price/quantity calculations
- Missing sequence number validation in WebSocket streams

**4. Risk Management Gaps**
- Missing or insufficient position size limits
- No circuit breaker / kill switch on consecutive losses
- No max drawdown protection
- Delta hedging gaps
- Missing fee accounting in profit calculations

**5. Performance Issues**
- Synchronous operations blocking the event loop
- Unnecessary data copies or conversions
- Inefficient data structures (lists where dicts/sets would be O(1))
- Excessive logging in hot paths

**6. Configuration & Operational Gaps**
- Hardcoded values that should be configurable
- Missing validation of config values (negative thresholds, etc.)
- No health check / heartbeat mechanism
- Missing graceful shutdown for all components

**7. Exchange Integration Issues**
- API rate limit handling
- Order status reconciliation gaps
- Missing handling of exchange maintenance / downtime
- Incorrect fee tier assumptions

**8. Missing Features (Critical)**
- Features that are referenced in code but not implemented (stubs, TODOs, NotImplementedError)
- Strategies or modes that are partially built

### Output Format:

After completing both phases, provide a structured report:

```
## Cleanup Summary
- Files modified: X
- Lines removed: ~Y
- Dead functions removed: [list]
- Dead imports cleaned: [count]
- Dead files found: [list]

## Deficiency Report

### CRITICAL (could cause financial loss or system crash)
1. [Issue] - [File:Line] - [Description] - [Recommended fix]

### HIGH (could cause incorrect behavior)
1. [Issue] - [File:Line] - [Description] - [Recommended fix]

### MEDIUM (suboptimal but functional)
1. [Issue] - [File:Line] - [Description] - [Recommended fix]

### LOW (code quality / maintainability)
1. [Issue] - [File:Line] - [Description] - [Recommended fix]
```

---

## Important Constraints

- **DO NOT** change any working logic — only remove dead code
- **DO NOT** refactor or "improve" code style during cleanup
- **DO NOT** add new features or abstractions
- **DO NOT** modify `.env` or configuration files
- **DO** make targeted, minimal deletions
- **DO** verify each removal doesn't break imports or references
- **DO** report deficiencies without fixing them (unless user asks)
- Work file by file, committing logical groups of changes
