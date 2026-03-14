---
name: arb-real-launch
description: Drive the controlled transition of the BrickTrade arbitrage bot from monitoring to real-profit trading. Use when asked to prepare for real trading, finalize PnL tests, validate full workflow across modes, calibrate fees/slippage from logs, rebuild conservative risk settings in `.env`, and run a staged rollout (monitoring → dry-run → real pilot → review).
---

# Arb Real Launch

## Overview

Execute a safe, repeatable launch process for the arbitrage bot: lock safe mode, finish validation, calibrate costs, tighten risk, run a long dry-run, then a small real pilot with checkpoints.

## Workflow Decision Tree

If `.env` indicates real trading is enabled:
1. First move to safe mode before any other action.
2. Only resume progression after tests and validations pass.

If tests are failing or missing:
1. Fix tests (PnL/orderbook freshness).
2. Re-run tests before changing risk or enabling real trading.

## Step 0: Establish Safe Mode

1. Inspect `.env` for:
   - `ARB_MONITORING_ONLY`
   - `ARB_DRY_RUN_MODE`
2. If real trading is enabled, set safe mode:
   - Monitoring-only: `ARB_MONITORING_ONLY=true`, `ARB_DRY_RUN_MODE=false`
   - Dry-run: `ARB_MONITORING_ONLY=false`, `ARB_DRY_RUN_MODE=true`
3. Restart bot if it is running.

Never enable real trading without explicit user confirmation in the current session.

## Step 1: Close PnL Test Gap

1. Read `CORRECTNESS_REPORT.md` to confirm current PnL test status.
2. Locate failing tests and ensure orderbook is refreshed before exit price checks.
3. Re-run the relevant tests:
   - `python -m pytest -q tests/test_system_*.py`
4. Report pass/fail with exact failing test names and reasons.

## Step 2: Full Workflow Validation

Verify all transitions and recovery:
1. Monitoring → dry-run → real (simulation only unless explicitly approved).
2. Restart recovery: verify positions and state restoration.
3. Check logs in `logs/` for API errors, partial fills, or mismatched positions.

## Step 3: Calibrate Fees, Funding, Slippage

Use real logs to calibrate:
1. Parse `logs/arbitrage_*.log` for:
   - `actual_*_fill_price`
   - fees and funding
   - slippage vs expected fill
2. Summarize:
   - Average fee per side
   - Funding impact
   - Slippage distribution
3. Adjust thresholds based on observed net profitability.

## Step 4: Rebuild Conservative Risk Profile

Set conservative `.env` parameters (start strict, loosen later):
- `POSITION_SIZE`
- `MAX_RISK_PER_TRADE`
- `ENTRY_THRESHOLD`
- `EXIT_THRESHOLD`
- `MIN_SPREAD`

Cross-check with `SWITCH_TO_TRADING_MODE.md`.

## Step 5: Long Dry-Run

Run dry-run for at least 1 week:
1. Collect:
   - signal frequency
   - drop/failed execution rate
   - net PnL estimate
   - API error rate
2. Produce a summary report with daily stats and a final decision.

## Step 6: Real Pilot (Small Deposit)

Only after passing dry-run and user approval:
1. Enable real mode with strict limits.
2. Use minimal position size and low risk.
3. Monitor continuously during first 48 hours.

## Step 7: Checkpoint Review

After 1–2 weeks:
1. Compare expected vs actual net PnL.
2. Recalibrate thresholds and position size.
3. Decide to scale, pause, or revert to dry-run.

## Required Files

- `.env`
- `CORRECTNESS_REPORT.md`
- `SWITCH_TO_TRADING_MODE.md`
- `QUICK_START_ARBITRAGE.md`
- `logs/`
- `tests/`
