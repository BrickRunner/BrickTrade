"""
Evening Auto-Calibrator: parses daily logs to extract real trading metrics
and recommends parameter adjustments for the next trading day.

Run at end of trading day (or on a schedule) to:
1. Parse today's logs for slippage, latency, spreads, 429s, fills
2. Compute statistics (median, p95, etc.)
3. Suggest tuned parameters for config
4. Save calibration report to logs/calibration/YYYY-MM-DD.json

Usage:
    from arbitrage.system.calibrator import DailyCalibrator
    calibrator = DailyCalibrator()
    report = await calibrator.run()   # parses today's logs
    report = await calibrator.run("2026-03-24")  # specific date
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_system")


@dataclass
class CalibrationMetrics:
    """Raw metrics extracted from logs."""
    slippage_bps: List[float] = field(default_factory=list)
    latency_ms: List[float] = field(default_factory=list)
    spreads_pct: List[float] = field(default_factory=list)
    rate_limit_429s: Dict[str, int] = field(default_factory=dict)  # exchange -> count
    fills: int = 0
    rejects: int = 0
    hedge_events: int = 0
    circuit_breaker_trips: Dict[str, int] = field(default_factory=dict)
    entry_near_misses: int = 0
    errors: int = 0


@dataclass
class CalibrationReport:
    """Calibration output with recommendations."""
    date: str
    metrics: Dict[str, Any]
    recommendations: Dict[str, Any]
    applied: bool = False


# ── Log line patterns ────────────────────────────────────────────────────

_RE_SLIPPAGE = re.compile(
    r"slippage[_=:]?\s*([\d.]+)\s*bps", re.IGNORECASE
)
_RE_LATENCY = re.compile(
    r"latency[_=:]?\s*([\d.]+)\s*ms", re.IGNORECASE
)
_RE_SPREAD = re.compile(
    r"spread[_=:]?\s*([\d.]+)\s*%", re.IGNORECASE
)
_RE_SPREAD_NET = re.compile(
    r"net_spread[_=:]?\s*([\d.-]+)", re.IGNORECASE
)
_RE_SPREAD_NEAR_MISS = re.compile(
    r"\[SPREAD_NEAR_MISS\]", re.IGNORECASE
)
_RE_429 = re.compile(
    r"429 from (\w+)", re.IGNORECASE
)
_RE_FILL = re.compile(
    r"(fill|filled|execution_success|opened_position)", re.IGNORECASE
)
_RE_REJECT = re.compile(
    r"(execution_reject|margin_reject|insufficient)", re.IGNORECASE
)
_RE_HEDGE = re.compile(
    r"(hedge_needed|hedging|fill_recovery)", re.IGNORECASE
)
_RE_CIRCUIT_TRIP = re.compile(
    r"circuit_breaker.*TRIPPED.*for (\w+)", re.IGNORECASE
)
_RE_ERROR = re.compile(
    r"\bERROR\b", re.IGNORECASE
)


class DailyCalibrator:
    """Parse daily logs and produce calibration recommendations."""

    def __init__(self, log_dir: str = "logs", output_dir: str = "logs/calibration"):
        self.log_dir = Path(log_dir)
        self.output_dir = Path(output_dir)

    async def run(self, date_str: Optional[str] = None) -> CalibrationReport:
        """Run calibration for a given date (default: today)."""
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        metrics = self._parse_logs(date_str)
        stats = self._compute_stats(metrics)
        recommendations = self._generate_recommendations(stats, metrics)
        report = CalibrationReport(
            date=date_str,
            metrics=stats,
            recommendations=recommendations,
        )
        self._save_report(report)
        logger.info(
            "calibrator: report for %s — %d fills, %d rejects, %d 429s, %d recommendations",
            date_str, metrics.fills, metrics.rejects,
            sum(metrics.rate_limit_429s.values()),
            len(recommendations),
        )
        return report

    def _parse_logs(self, date_str: str) -> CalibrationMetrics:
        """Parse all log files for the given date."""
        metrics = CalibrationMetrics()
        date_dir = self.log_dir / date_str

        if not date_dir.exists():
            logger.warning("calibrator: no log directory for %s", date_str)
            return metrics

        # Walk all hour subdirectories
        for hour_dir in sorted(date_dir.iterdir()):
            if not hour_dir.is_dir():
                continue
            for log_file in hour_dir.iterdir():
                if log_file.suffix != ".log":
                    continue
                self._parse_file(log_file, metrics)

        return metrics

    def _parse_file(self, path: Path, metrics: CalibrationMetrics) -> None:
        """Parse a single log file and accumulate metrics."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    self._parse_line(line, metrics)
        except OSError as exc:
            logger.warning("calibrator: cannot read %s: %s", path, exc)

    def _parse_line(self, line: str, m: CalibrationMetrics) -> None:
        """Extract metrics from a single log line."""
        # Slippage
        match = _RE_SLIPPAGE.search(line)
        if match:
            try:
                m.slippage_bps.append(float(match.group(1)))
            except ValueError:
                pass

        # Latency
        match = _RE_LATENCY.search(line)
        if match:
            try:
                m.latency_ms.append(float(match.group(1)))
            except ValueError:
                pass

        # Spread
        match = _RE_SPREAD.search(line)
        if match:
            try:
                m.spreads_pct.append(float(match.group(1)))
            except ValueError:
                pass

        # Net spread (alternative format)
        match = _RE_SPREAD_NET.search(line)
        if match:
            try:
                m.spreads_pct.append(float(match.group(1)))
            except ValueError:
                pass

        # Near misses
        if _RE_SPREAD_NEAR_MISS.search(line):
            m.entry_near_misses += 1

        # 429 rate limits
        match = _RE_429.search(line)
        if match:
            exchange = match.group(1).lower()
            m.rate_limit_429s[exchange] = m.rate_limit_429s.get(exchange, 0) + 1

        # Fills
        if _RE_FILL.search(line):
            m.fills += 1

        # Rejects
        if _RE_REJECT.search(line):
            m.rejects += 1

        # Hedge events
        if _RE_HEDGE.search(line):
            m.hedge_events += 1

        # Circuit breaker trips
        match = _RE_CIRCUIT_TRIP.search(line)
        if match:
            ex = match.group(1).lower()
            m.circuit_breaker_trips[ex] = m.circuit_breaker_trips.get(ex, 0) + 1

        # General errors
        if _RE_ERROR.search(line):
            m.errors += 1

    def _compute_stats(self, m: CalibrationMetrics) -> Dict[str, Any]:
        """Compute summary statistics from raw metrics."""
        stats: Dict[str, Any] = {
            "fills": m.fills,
            "rejects": m.rejects,
            "hedge_events": m.hedge_events,
            "entry_near_misses": m.entry_near_misses,
            "errors": m.errors,
            "rate_limit_429s": dict(m.rate_limit_429s),
            "circuit_breaker_trips": dict(m.circuit_breaker_trips),
        }

        if m.slippage_bps:
            stats["slippage"] = {
                "count": len(m.slippage_bps),
                "median": round(statistics.median(m.slippage_bps), 2),
                "mean": round(statistics.mean(m.slippage_bps), 2),
                "p95": round(self._percentile(m.slippage_bps, 95), 2),
                "max": round(max(m.slippage_bps), 2),
            }

        if m.latency_ms:
            stats["latency"] = {
                "count": len(m.latency_ms),
                "median": round(statistics.median(m.latency_ms), 2),
                "mean": round(statistics.mean(m.latency_ms), 2),
                "p95": round(self._percentile(m.latency_ms, 95), 2),
                "max": round(max(m.latency_ms), 2),
            }

        if m.spreads_pct:
            stats["spreads"] = {
                "count": len(m.spreads_pct),
                "median": round(statistics.median(m.spreads_pct), 4),
                "mean": round(statistics.mean(m.spreads_pct), 4),
                "p95": round(self._percentile(m.spreads_pct, 95), 4),
                "max": round(max(m.spreads_pct), 4),
            }

        return stats

    def _generate_recommendations(
        self, stats: Dict[str, Any], m: CalibrationMetrics
    ) -> Dict[str, Any]:
        """Generate parameter tuning recommendations based on observed data."""
        recs: Dict[str, Any] = {}

        # ── Slippage tuning ──────────────────────────────────
        if "slippage" in stats:
            slip = stats["slippage"]
            # If p95 slippage > 12 bps, recommend increasing max_order_slippage_bps
            if slip["p95"] > 12:
                recs["RISK_MAX_SLIPPAGE_BPS"] = {
                    "current_default": 12.0,
                    "recommended": round(slip["p95"] * 1.3, 1),
                    "reason": f"P95 slippage ({slip['p95']} bps) exceeds current limit",
                }
            # If slippage consistently low, tighten
            elif slip["p95"] < 5 and slip["count"] >= 10:
                recs["RISK_MAX_SLIPPAGE_BPS"] = {
                    "current_default": 12.0,
                    "recommended": round(max(slip["p95"] * 2, 6.0), 1),
                    "reason": f"Slippage consistently low (P95={slip['p95']} bps), can tighten",
                }

        # ── Latency tuning ───────────────────────────────────
        if "latency" in stats:
            lat = stats["latency"]
            if lat["p95"] > 500:
                recs["ARB_MAX_LATENCY_MS"] = {
                    "current_default": 400.0,
                    "recommended": round(lat["p95"] * 1.5, 0),
                    "reason": f"P95 latency ({lat['p95']}ms) too high — widen or investigate connection",
                }
            elif lat["p95"] < 100 and lat["count"] >= 20:
                recs["ARB_MAX_LATENCY_MS"] = {
                    "current_default": 400.0,
                    "recommended": round(max(lat["p95"] * 3, 200.0), 0),
                    "reason": f"P95 latency low ({lat['p95']}ms), can tighten for faster rejection",
                }

        # ── Spread tuning ────────────────────────────────────
        if "spreads" in stats and stats["spreads"]["count"] >= 5:
            sp = stats["spreads"]
            # If many near misses, recommend lowering min_spread
            if m.entry_near_misses > m.fills * 3 and m.fills < 5:
                recs["ARB_MIN_SPREAD_PCT"] = {
                    "current_default": 0.50,
                    "recommended": round(max(sp["median"] * 0.8, 0.10), 2),
                    "reason": f"{m.entry_near_misses} near-misses vs {m.fills} fills — lower threshold to capture more",
                }

        # ── Rate limiter tuning ──────────────────────────────
        total_429 = sum(m.rate_limit_429s.values())
        if total_429 > 50:
            worst_exchange = max(m.rate_limit_429s, key=m.rate_limit_429s.get)
            recs["RATE_LIMITER"] = {
                "exchange": worst_exchange,
                "429_count": m.rate_limit_429s[worst_exchange],
                "recommended_action": "Lower request rate for this exchange",
                "reason": f"Too many 429s ({total_429} total, {m.rate_limit_429s[worst_exchange]} from {worst_exchange})",
            }

        # ── Circuit breaker ──────────────────────────────────
        for ex, trips in m.circuit_breaker_trips.items():
            if trips >= 3:
                recs[f"CIRCUIT_BREAKER_{ex.upper()}"] = {
                    "trips": trips,
                    "recommended_action": "Increase cooldown or investigate exchange stability",
                    "reason": f"{ex} tripped circuit breaker {trips} times",
                }

        # ── Hedge rate ───────────────────────────────────────
        if m.fills > 0 and m.hedge_events / max(m.fills, 1) > 0.3:
            recs["HEDGE_QUALITY"] = {
                "hedge_rate": round(m.hedge_events / m.fills, 2),
                "recommended_action": "Consider switching to maker-taker execution",
                "reason": f"Hedge events ({m.hedge_events}) are {round(m.hedge_events/m.fills*100)}% of fills",
            }

        return recs

    def _save_report(self, report: CalibrationReport) -> None:
        """Save calibration report to JSON."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{report.date}.json"
        data = {
            "date": report.date,
            "generated_at": datetime.now().isoformat(),
            "metrics": report.metrics,
            "recommendations": report.recommendations,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info("calibrator: saved report to %s", path)
        except OSError as exc:
            logger.error("calibrator: failed to save report: %s", exc)

    @staticmethod
    def _percentile(data: List[float], pct: float) -> float:
        """Compute percentile (0-100) for a sorted list."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = (pct / 100) * (len(sorted_data) - 1)
        lower = int(idx)
        upper = min(lower + 1, len(sorted_data) - 1)
        weight = idx - lower
        return sorted_data[lower] * (1 - weight) + sorted_data[upper] * weight
