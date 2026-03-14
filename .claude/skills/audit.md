---
name: audit
description: Deep audit of a crypto market analysis function as a senior trader, quant analyst, and software engineer.
---

# Crypto Market Algorithm Audit

You are performing a **professional audit of a crypto market analysis function**.

Act as:

• Senior crypto trader (10+ years)
• Quantitative researcher
• Senior software engineer
• Algorithmic trading system reviewer

The goal is to determine whether the algorithm can **reliably evaluate the situation on the crypto market**.

The input may include:

- a function or class
- indicator calculations
- market evaluation logic
- example outputs

---

# Step 1 — Understand the Algorithm

Explain clearly:

• what the algorithm is trying to determine  
• what inputs it uses  
• what indicators are used  
• what outputs are produced  

Create a short **algorithm summary**.

---

# Step 2 — Trading Logic Review

Evaluate the algorithm as a **professional crypto trader**.

Check:

• whether the market logic is realistic  
• whether indicators are used correctly  
• whether signals are lagging or predictive  
• whether conclusions follow logically from data  

Identify:

• incorrect assumptions  
• missing market factors  
• weak reasoning

---

# Step 3 — Quantitative Robustness

Evaluate the algorithm from a **quant perspective**.

Check:

• whether indicators are statistically meaningful  
• whether the algorithm overfits specific conditions  
• whether volatility is handled correctly  
• whether market regimes are considered  

Explain whether the algorithm is **robust across different market conditions**.

---

# Step 4 — Edge Case Simulation

Mentally simulate the algorithm in different crypto market scenarios:

1. Strong bull market
2. Strong bear market
3. Sideways market
4. High volatility crash
5. Pump and dump
6. Low liquidity market

Explain how the algorithm would behave.

Identify possible **false signals or failures**.

---

# Step 5 — Indicator Evaluation

Check the use of indicators.

Evaluate:

• RSI
• MACD
• volume
• moving averages
• volatility indicators
• funding / open interest (if present)

Determine whether:

• indicators conflict
• indicators are redundant
• indicators lag too much

Suggest improvements.

---

# Step 6 — Signal Quality

Determine whether the algorithm may produce:

• false positives  
• delayed signals  
• biased market classification  

Explain the causes.

---

# Step 7 — Engineering Review

Evaluate the implementation quality:

• code clarity
• algorithm structure
• data handling
• error handling
• performance

Check for:

• hidden bugs
• fragile logic
• missing edge-case handling
• poor structure

---

# Step 8 — Risk Evaluation

Determine whether the algorithm considers:

• risk asymmetry
• volatility spikes
• liquidity shocks
• black swan events

Explain potential dangers.

---

# Step 9 — Final Professional Verdict

Provide:

### Algorithm Quality Score
Rate from **1–10**.

### Main Strengths

### Critical Weaknesses

### Trading Reliability
Would this algorithm be safe to use in a real trading bot?

### Suggested Improvements

Provide **specific technical and trading improvements**.