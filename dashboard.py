"""
Streamlit Dashboard for BrickTrade Arbitrage Bot.

Run:
    streamlit run dashboard.py

Reads data from:
    - logs/calibration/*.json     (calibration reports)
    - logs/YYYY-MM-DD/HH/*.log   (trade logs for slippage, spreads, fills)
    - data/open_positions.json    (current open positions)
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
CALIBRATION_DIR = LOG_DIR / "calibration"
POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "data/open_positions.json"))

_RE_SLIPPAGE = re.compile(r"slippage[_=:]?\s*([\d.]+)\s*bps", re.IGNORECASE)
_RE_LATENCY = re.compile(r"latency[_=:]?\s*([\d.]+)\s*ms", re.IGNORECASE)
_RE_SPREAD = re.compile(r"(?:net_)?spread[_=:]?\s*([\d.-]+)", re.IGNORECASE)
_RE_FILL = re.compile(r"(fill|filled|execution_success|opened_position)", re.IGNORECASE)
_RE_REJECT = re.compile(r"(execution_reject|margin_reject|insufficient)", re.IGNORECASE)
_RE_FUNDING = re.compile(r"funding[_=:]?\s*([\d.-]+)", re.IGNORECASE)
_RE_PNL = re.compile(r"realized_pnl[_=:]?\s*([\d.-]+)", re.IGNORECASE)
_RE_SIGNAL = re.compile(
    r"\[(CROSS_EXCHANGE_SIGNAL|CASH_CARRY_SIGNAL|FUNDING_SIGNAL)\].*?"
    r"(\w+USDT).*?on\s+(\w+)",
    re.IGNORECASE,
)


def _available_dates() -> List[str]:
    """Return sorted list of date directories in logs/."""
    if not LOG_DIR.exists():
        return []
    dates = []
    for d in LOG_DIR.iterdir():
        if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", d.name):
            dates.append(d.name)
    return sorted(dates, reverse=True)


def _parse_logs_for_date(date_str: str) -> Dict[str, Any]:
    """Parse all logs for a given date, returning structured data."""
    date_dir = LOG_DIR / date_str
    data: Dict[str, Any] = {
        "slippage": [],
        "latency": [],
        "spreads": [],
        "fills": 0,
        "rejects": 0,
        "funding_rates": [],
        "pnl_entries": [],
        "signals": [],
        "hours": {},
    }
    if not date_dir.exists():
        return data

    for hour_dir in sorted(date_dir.iterdir()):
        if not hour_dir.is_dir():
            continue
        hour = hour_dir.name
        hour_fills = 0
        hour_rejects = 0
        for log_file in hour_dir.iterdir():
            if log_file.suffix != ".log":
                continue
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = _RE_SLIPPAGE.search(line)
                        if m:
                            data["slippage"].append(float(m.group(1)))
                        m = _RE_LATENCY.search(line)
                        if m:
                            data["latency"].append(float(m.group(1)))
                        m = _RE_SPREAD.search(line)
                        if m:
                            try:
                                data["spreads"].append(float(m.group(1)))
                            except ValueError:
                                pass
                        if _RE_FILL.search(line):
                            data["fills"] += 1
                            hour_fills += 1
                        if _RE_REJECT.search(line):
                            data["rejects"] += 1
                            hour_rejects += 1
                        m = _RE_FUNDING.search(line)
                        if m:
                            try:
                                data["funding_rates"].append(float(m.group(1)))
                            except ValueError:
                                pass
                        m = _RE_PNL.search(line)
                        if m:
                            try:
                                data["pnl_entries"].append(float(m.group(1)))
                            except ValueError:
                                pass
                        m = _RE_SIGNAL.search(line)
                        if m:
                            data["signals"].append({
                                "type": m.group(1),
                                "symbol": m.group(2),
                                "exchange": m.group(3),
                                "hour": hour,
                            })
            except OSError:
                continue
        data["hours"][hour] = {"fills": hour_fills, "rejects": hour_rejects}
    return data


def _load_calibration(date_str: str) -> Dict[str, Any] | None:
    path = CALIBRATION_DIR / f"{date_str}.json"
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _load_positions() -> List[Dict]:
    if POSITIONS_FILE.exists():
        try:
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="BrickTrade Dashboard", layout="wide")
st.title("BrickTrade Arbitrage Dashboard")

# Sidebar — date picker
dates = _available_dates()
if not dates:
    st.warning("No log data found. Start the bot and wait for logs to accumulate in `logs/`.")
    st.stop()

selected_date = st.sidebar.selectbox("Date", dates, index=0)
log_data = _parse_logs_for_date(selected_date)
calibration = _load_calibration(selected_date)

# Top KPIs
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Fills", log_data["fills"])
col2.metric("Rejects", log_data["rejects"])
fill_rate = (
    log_data["fills"] / max(log_data["fills"] + log_data["rejects"], 1) * 100
)
col3.metric("Fill Rate", f"{fill_rate:.1f}%")
total_pnl = sum(log_data["pnl_entries"])
col4.metric("Realized PnL", f"${total_pnl:+.2f}")
col5.metric("Signals", len(log_data["signals"]))

st.divider()

# --- PnL Equity Curve ---
st.subheader("PnL Equity Curve")
if log_data["pnl_entries"]:
    import pandas as pd
    cumulative = []
    running = 0.0
    for p in log_data["pnl_entries"]:
        running += p
        cumulative.append(running)
    df_pnl = pd.DataFrame({"Trade #": range(1, len(cumulative) + 1), "Cumulative PnL ($)": cumulative})
    st.line_chart(df_pnl, x="Trade #", y="Cumulative PnL ($)")
else:
    st.info("No PnL data for this date.")

# --- Two columns: Slippage + Spread ---
left, right = st.columns(2)

with left:
    st.subheader("Slippage Distribution (bps)")
    if log_data["slippage"]:
        import pandas as pd
        df_slip = pd.DataFrame({"Slippage (bps)": log_data["slippage"]})
        st.bar_chart(df_slip["Slippage (bps)"].value_counts().sort_index())
        st.caption(
            f"Median: {sorted(log_data['slippage'])[len(log_data['slippage'])//2]:.1f} bps | "
            f"Max: {max(log_data['slippage']):.1f} bps | "
            f"Count: {len(log_data['slippage'])}"
        )
    else:
        st.info("No slippage data.")

with right:
    st.subheader("Spread Distribution (%)")
    if log_data["spreads"]:
        import pandas as pd
        df_sp = pd.DataFrame({"Spread": log_data["spreads"]})
        st.bar_chart(df_sp["Spread"].value_counts().sort_index())
        st.caption(
            f"Median: {sorted(log_data['spreads'])[len(log_data['spreads'])//2]:.4f} | "
            f"Max: {max(log_data['spreads']):.4f} | "
            f"Count: {len(log_data['spreads'])}"
        )
    else:
        st.info("No spread data.")

# --- Spread Heatmap (fills per hour) ---
st.subheader("Activity Heatmap (fills per hour)")
if log_data["hours"]:
    import pandas as pd
    hours = sorted(log_data["hours"].keys())
    fills_per_hour = [log_data["hours"][h]["fills"] for h in hours]
    rejects_per_hour = [log_data["hours"][h]["rejects"] for h in hours]
    df_heat = pd.DataFrame({"Hour": hours, "Fills": fills_per_hour, "Rejects": rejects_per_hour})
    st.bar_chart(df_heat, x="Hour", y=["Fills", "Rejects"])
else:
    st.info("No hourly data.")

# --- Funding Rate Tracker ---
st.subheader("Funding Rates Observed")
if log_data["funding_rates"]:
    import pandas as pd
    df_fund = pd.DataFrame({"#": range(1, len(log_data["funding_rates"]) + 1), "Rate (%)": log_data["funding_rates"]})
    st.line_chart(df_fund, x="#", y="Rate (%)")
else:
    st.info("No funding rate data in logs.")

# --- Latency ---
st.subheader("API Latency (ms)")
if log_data["latency"]:
    import pandas as pd
    df_lat = pd.DataFrame({"Latency (ms)": log_data["latency"]})
    st.line_chart(df_lat)
    sorted_lat = sorted(log_data["latency"])
    p95_idx = int(0.95 * (len(sorted_lat) - 1))
    st.caption(
        f"Median: {sorted_lat[len(sorted_lat)//2]:.0f}ms | "
        f"P95: {sorted_lat[p95_idx]:.0f}ms | "
        f"Max: {max(sorted_lat):.0f}ms"
    )
else:
    st.info("No latency data.")

# --- Open Positions ---
st.divider()
st.subheader("Open Positions")
positions = _load_positions()
if positions:
    import pandas as pd
    df_pos = pd.DataFrame(positions)
    display_cols = [c for c in ["position_id", "strategy_id", "symbol", "long_exchange",
                                 "short_exchange", "notional_usd", "unrealized_pnl", "opened_at"] if c in df_pos.columns]
    st.dataframe(df_pos[display_cols], use_container_width=True)
else:
    st.info("No open positions.")

# --- Calibration Report ---
st.divider()
st.subheader("Calibration Report")
if calibration:
    col_a, col_b = st.columns(2)
    with col_a:
        st.write("**Metrics**")
        st.json(calibration.get("metrics", {}))
    with col_b:
        st.write("**Recommendations**")
        recs = calibration.get("recommendations", {})
        if recs:
            for key, val in recs.items():
                st.warning(f"**{key}**: {val.get('reason', '')}")
        else:
            st.success("No recommendations — parameters look good.")
else:
    st.info(f"No calibration report for {selected_date}.")

# --- Signals Table ---
if log_data["signals"]:
    st.divider()
    st.subheader("Strategy Signals")
    import pandas as pd
    df_sig = pd.DataFrame(log_data["signals"])
    st.dataframe(df_sig, use_container_width=True)
